import anvil.server
import json
from datetime import date, datetime, timedelta

from .daily_ship_plan import (get_connection, _parse_date, _get_params,
make_serializable, _agent_key_ok)

# =============================================================================
# TEACHING NUGGETS  (nugget-1.0.0, 2026-09-17)
#
# Dave, 2026-09-16: the agent "must act as an expert in production control
# and a graduate-level professor - both directing and instructing." The
# students are everyone (PC, assemblers, Sales) and "training time has to be
# in brief nuggets." So: one two-sentence lesson per plan day, named by
# concept, at the top of the morning narrative, picked by rule from
# pc_teaching_nugget (migration 073) and logged per day + channel in
# pc_teaching_nugget_log so nothing repeats inside NUGGET_REPEAT_DAYS and
# the Friday digest can say what was taught.
#
# Channels: MEETING  - the narrative (Agent proposal popup / the 9 am board);
#                      audiences ALL, PC, SALES, OFFICE (= PC + Sales)
#           ASSEMBLY - the floor (SE Dept App label, when Dave adds it);
#                      audiences ALL, ASSEMBLY
#
# Pick order, per channel (every step is explainable from the log row):
#   0. a lesson already logged for the day  -> the same one (idempotent, so
#      a narrative re-run or a console call never changes the day's lesson)
#   1. a FOUNDATION (seq <= NUGGET_FOUNDATION_SEQ) never shown -> lowest seq
#   2. a fired TRIGGER whose lesson is outside the repeat window -> highest
#      trigger_priority, then lowest seq. Trigger and curriculum days
#      alternate: after a trigger day only a priority >= 80 trigger may
#      take the next day as well.
#   3. CURRICULUM: never shown on this channel -> lowest seq
#   4. REPEAT: least recently shown
#
# Live numbers: the body may carry {placeholders} (fill %, horizon ...);
# the optional live_line is a third sentence with numbers from the latest
# ledger row, this morning's run, holds and open retro questions. It is
# appended only when every number it names is available and
# NUGGET_LIVE_NUMBERS = 1. The ledger row is the previous *computed* day
# (the 08:45 ledger runs after the 06:30 pick), and the line names it.
#
# Nothing here writes anywhere but pc_teaching_nugget_log (and, through
# nugget_save, the library itself when a person edits a lesson).
# =============================================================================

_NUGGET_VERSION = 'nugget-1.0.0'
_CHANNELS = ('MEETING', 'ASSEMBLY')
_AUDIENCE = {'MEETING': ('ALL', 'PC', 'SALES', 'OFFICE'),
             'ASSEMBLY': ('ALL', 'ASSEMBLY')}
_DEFAULT_THRESHOLDS = {'ATTENDANCE_LOW': 85, 'ATTAINMENT_LOW': 25, 'PLAN_OVERLOAD': 110,
                       'RUSH_HEAVY': 50, 'REVISIONS_MANY': 3, 'NO_ESTIMATE_MANY': 10,
                       'EFF_DRIFT': 15, 'GATED_MANY': 40, 'ZERO_TOUCH_SKIPPED': 20}
_TRIGGER_TWO_DAYS_MIN_PRIORITY = 80      # may follow another trigger day
_LEDGER_MAX_AGE_BDAYS = 4                # older ledger rows do not fire triggers


# -- dates -------------------------------------------------------------

def _next_business_day(d):
  d = d + timedelta(days=1)
  while d.weekday() >= 5:
    d += timedelta(days=1)
  return d


def _prev_business_day(d):
  d = d - timedelta(days=1)
  while d.weekday() >= 5:
    d -= timedelta(days=1)
  return d


def _minus_business_days(d, n):
  for _ in range(max(0, int(n))):
    d = _prev_business_day(d)
  return d


def _business_days_between(a, b):
  """Business days from a to b (a < b); 0 when b <= a."""
  n, d = 0, a
  while d < b:
    d += timedelta(days=1)
    if d.weekday() < 5:
      n += 1
  return n


def _week_of(d):
  mon = d - timedelta(days=d.weekday())
  return mon, mon + timedelta(days=4)


# -- small helpers -----------------------------------------------------

def _f(v, default=None):
  try:
    return float(v)
  except (TypeError, ValueError):
    return default


def _obj_exists(cursor, name):
  cursor.execute("SELECT OBJECT_ID(%s) AS o", (f'dbo.{name}',))
  return cursor.fetchone()['o'] is not None


def _table_ready(cursor):
  return _obj_exists(cursor, 'pc_teaching_nugget') and _obj_exists(cursor, 'pc_teaching_nugget_log')


def _release(conn):
  """Read-only paths: end the implicit transaction before the connection
  is parked in the pool (pymssql runs with autocommit off)."""
  try:
    conn.rollback()
  except Exception:
    pass
  conn.close()


def _nugget_params(p):
  def _num(k, d):
    v = _f(p.get(k), None)
    return d if v is None else v
  thresholds = dict(_DEFAULT_THRESHOLDS)
  for code in _DEFAULT_THRESHOLDS:               # one NUGGET_T_<CODE> row each
    fv = _f(p.get('NUGGET_T_' + code), None)
    if fv is not None:
      thresholds[code] = fv
  return {
    'enabled': (p.get('NUGGET_ENABLED') or '1').strip() == '1',
    'repeat_days': int(_num('NUGGET_REPEAT_DAYS', 20)),
    'foundation_seq': int(_num('NUGGET_FOUNDATION_SEQ', 60)),
    'live_numbers': (p.get('NUGGET_LIVE_NUMBERS') or '1').strip() == '1',
    'digest_day': (p.get('NUGGET_DIGEST_DAY') or 'Fri').strip(),
    'digest_time': (p.get('NUGGET_DIGEST_TIME') or '15:30').strip(),
    'thresholds': thresholds,
  }


def _s_int(v):
  return None if v is None else f"{int(round(float(v))):,}"


def _s_1(v):
  return None if v is None else f"{float(v):,.1f}"


def _s_2(v):
  return None if v is None else f"{float(v):.2f}"


def _s_3(v):
  return None if v is None else f"{float(v):.3f}"


def _s_pct(v):
  return None if v is None else f"{float(v):.0f}"


def _s_day(d):
  return d.strftime('%a %m/%d') if d else None


# -- the numbers a lesson may quote ------------------------------------

def _numbers(cursor, plan_date, run, p, np):
  """Two dicts: text (formatted strings for {placeholders}, None dropped)
  and raw (floats/ints for the triggers)."""
  run = run or {}
  raw, text = {}, {}

  # settings - always present, so a body placeholder always fills
  fill = _f(p.get('AGENT_FILL_PCT'), 80)
  eff = _f(p.get('EFFICIENCY_FACTOR'), 1.2)
  text.update({
    'plan_day': _s_day(plan_date),
    'fill_pct': _s_pct(fill),
    'stretch_lines': _s_int(_f(p.get('AGENT_STRETCH_LINES'), 5)),
    'max_line_pct': _s_pct(_f(p.get('AGENT_MAX_LINE_PCT'), 50)),
    'horizon_days': _s_int(_f(p.get('PLAN_HORIZON_DAYS'), 3)),
    'efficiency_used': _s_2(eff),
    'hours_per_day': f"{_f(p.get('HOURS_PER_DAY'), 8.0):g}",
    'tolerance_pct': _s_pct(_f(p.get('LEDGER_TOLERANCE_PCT'), 10)),
    'window_days': _s_int(_f(p.get('LEDGER_WINDOW_DAYS'), 5)),
    'hold_days': _s_int(_f(p.get('HOLD_DEFAULT_DAYS'), 5)),
    'retro_max_q': _s_int(_f(p.get('RETRO_MAX_QUESTIONS'), 3)),
    'retro_unknown_days': _s_int(_f(p.get('RETRO_UNKNOWN_AFTER_DAYS'), 5)),
    'retro_unplanned_min': _s_int(_f(p.get('RETRO_UNPLANNED_MIN_INCHES'), 50)),
    'high_dollar': '15,000',
  })
  try:
    cursor.execute("SELECT threshold_value FROM dbo.pc_flag_definitions WHERE flag_code = 'B02'")
    r = cursor.fetchone()
    if r and _f(r['threshold_value']) is not None:
      text['high_dollar'] = _s_int(r['threshold_value'])
  except Exception:
    pass

  # this morning's run (from the service; a console call has none)
  fill_run = _f(run.get('fill_pct'))
  raw.update({
    'n_gated': _f(run.get('n_gated')),
    'n_split': _f(run.get('n_split_needed')),
    'n_stretch': _f(run.get('n_stretch')),
    'n_proposed': _f(run.get('n_proposed')),
    'n_zero_touch': _f(run.get('n_zero_touch_skipped')),
    'capacity_inches': _f(run.get('capacity_inches')),
    'target_inches': _f(run.get('target_inches')),
    'proposed_inches': _f(run.get('proposed_inches')),
  })
  if raw['target_inches'] is None and raw['capacity_inches'] and fill_run:
    raw['target_inches'] = raw['capacity_inches'] * (fill_run if fill_run <= 1 else fill_run / 100.0)
  for k in ('n_gated', 'n_split', 'n_stretch', 'n_proposed', 'n_zero_touch'):
    text[k] = _s_int(raw[k])
  for k in ('capacity_inches', 'target_inches', 'proposed_inches'):
    text[k] = _s_int(raw[k])

  # the latest ledger row before the plan day (069; optional)
  raw['ledger'] = None
  if _obj_exists(cursor, 'pc_capacity_ledger'):
    cursor.execute("""
        SELECT TOP 1 * FROM dbo.pc_capacity_ledger
        WHERE plan_date < %s AND plan_date >= %s
        ORDER BY plan_date DESC
    """, (plan_date, _minus_business_days(plan_date, _LEDGER_MAX_AGE_BDAYS)))
    L = cursor.fetchone()
    if L:
      raw['ledger'] = L
      shipped = _f(L.get('shipped_inches'))
      unplanned = _f(L.get('shipped_unplanned_inches'))
      unplanned_pct = (unplanned / shipped * 100.0) if shipped and unplanned is not None else None
      rolling = _f(L.get('rolling_ratio'))
      rolling_days = int(L.get('rolling_days') or 0)
      wb = L.get('within_band')
      if wb is None:
        band_text = 'not judged yet'
      elif wb:
        band_text = 'within the band'
      else:
        band_text = ('under the band' if rolling is not None and rolling < 1 else 'over the band')
      implied = (eff / rolling) if (rolling and rolling_days >= 3) else _f(L.get('implied_efficiency'))
      raw.update({
        'attendance_pct': _f(L.get('attendance_pct')),
        'attainment_pct': _f(L.get('attainment_pct')),
        'planned_final': _f(L.get('planned_final_inches')),
        'plan_load_pct': _f(L.get('plan_load_pct')),
        'unplanned_pct': unplanned_pct,
        'revision_no': _f(L.get('revision_no')),
        'n_no_estimate': _f(L.get('n_shipped_no_estimate')),
        'within_band': (None if wb is None else bool(wb)),
        'rolling_ratio': rolling, 'rolling_days': rolling_days,
        'implied_efficiency': implied, 'efficiency_used': eff,
      })
      ld = L.get('plan_date')
      ld = ld if isinstance(ld, date) else _parse_date(ld)
      text.update({
        'ledger_day': _s_day(ld),
        'est_capacity': _s_int(L.get('est_capacity_inches')),
        'published_capacity': _s_int(L.get('published_capacity_inches')),
        'published_crew': None if L.get('published_crew') is None else f"{float(L['published_crew']):g}",
        'actual_capacity': _s_int(L.get('actual_capacity_inches')),
        'actual_crew': None if L.get('actual_crew') is None else f"{float(L['actual_crew']):g}",
        'attendance_pct': _s_pct(L.get('attendance_pct')),
        'shipped_inches': _s_int(shipped),
        'shipped_planned': _s_int(L.get('shipped_planned_inches')),
        'unplanned_pct': _s_pct(unplanned_pct),
        'daily_ratio': _s_3(L.get('daily_ratio')),
        'rolling_ratio': _s_3(rolling),
        'rolling_days': str(rolling_days) if rolling_days else None,
        'band_text': band_text,
        'attainment_pct': _s_1(L.get('attainment_pct')),
        'plan_load_pct': _s_pct(L.get('plan_load_pct')),
        'revision_no': _s_int(L.get('revision_no')),
        'implied_efficiency': _s_2(implied),
        'n_no_estimate': _s_int(L.get('n_shipped_no_estimate')),
        'planned_final': _s_int(L.get('planned_final_inches')),
      })

  # material holds (071; optional)
  raw['n_holds'] = raw['n_holds_expiring'] = None
  if _obj_exists(cursor, 'pc_plan_line_hold'):
    cursor.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN expires_on <= %s THEN 1 ELSE 0 END) AS n_exp
        FROM dbo.pc_plan_line_hold
        WHERE released_at IS NULL AND expires_on >= %s
    """, (_next_business_day(plan_date), plan_date))
    r = cursor.fetchone()
    raw['n_holds'] = int(r['n'] or 0)
    raw['n_holds_expiring'] = int(r['n_exp'] or 0)
    text['n_holds'] = str(raw['n_holds'])
    text['n_holds_expiring'] = str(raw['n_holds_expiring'])

  # retro questions (053; optional)
  raw['n_open_q'] = raw['n_aged_q'] = None
  if _obj_exists(cursor, 'pc_plan_variance'):
    cursor.execute("""
        SELECT SUM(CASE WHEN asked_at IS NOT NULL AND answered_at IS NULL
                             AND resolution IS NULL THEN 1 ELSE 0 END) AS n_open,
               SUM(CASE WHEN resolution = 'Unknown'
                             AND resolved_at >= %s THEN 1 ELSE 0 END) AS n_aged
        FROM dbo.pc_plan_variance
        WHERE question_text IS NOT NULL AND plan_date >= %s
    """, (_minus_business_days(plan_date, 5), _minus_business_days(plan_date, 15)))
    r = cursor.fetchone()
    raw['n_open_q'] = int(r['n_open'] or 0)
    raw['n_aged_q'] = int(r['n_aged'] or 0)
    text['n_open_q'] = str(raw['n_open_q'])
    text['n_aged_q'] = str(raw['n_aged_q'])

  text = {k: v for k, v in text.items() if v is not None}
  return text, raw


# -- the triggers ------------------------------------------------------

def _triggers(raw, plan_date, thr):
  """Which trigger codes fire this morning. Missing numbers never fire."""
  def ge(k, code):
    v = raw.get(k)
    return v is not None and v >= thr.get(code, _DEFAULT_THRESHOLDS.get(code, 0))

  def gt(k, code):
    v = raw.get(k)
    return v is not None and v > thr.get(code, _DEFAULT_THRESHOLDS.get(code, 0))

  def lt(k, code):
    v = raw.get(k)
    return v is not None and v < thr.get(code, _DEFAULT_THRESHOLDS.get(code, 0))

  drift = None
  if raw.get('implied_efficiency') and raw.get('efficiency_used'):
    drift = abs(raw['implied_efficiency'] - raw['efficiency_used']) / raw['efficiency_used'] * 100.0
  fired = {
    'ATTENDANCE_LOW': lt('attendance_pct', 'ATTENDANCE_LOW'),
    'BAND_OUT': raw.get('within_band') is False,
    'BAND_IN': raw.get('within_band') is True,
    'ATTAINMENT_LOW': bool(raw.get('planned_final')) and lt('attainment_pct', 'ATTAINMENT_LOW'),
    'PLAN_OVERLOAD': gt('plan_load_pct', 'PLAN_OVERLOAD'),
    'RUSH_HEAVY': gt('unplanned_pct', 'RUSH_HEAVY'),
    'REVISIONS_MANY': ge('revision_no', 'REVISIONS_MANY'),
    'NO_ESTIMATE_MANY': ge('n_no_estimate', 'NO_ESTIMATE_MANY'),
    'EFF_DRIFT': drift is not None and drift > thr.get('EFF_DRIFT', 15),
    'GATED_MANY': ge('n_gated', 'GATED_MANY'),
    'SPLIT_PRESENT': bool(raw.get('n_split')),
    'ZERO_TOUCH_SKIPPED': ge('n_zero_touch', 'ZERO_TOUCH_SKIPPED'),
    'HOLD_ACTIVE': bool(raw.get('n_holds')),
    'HOLD_EXPIRING': bool(raw.get('n_holds_expiring')),
    'RETRO_OPEN': bool(raw.get('n_open_q')),
    'RETRO_AGED': bool(raw.get('n_aged_q')),
    'MONDAY': plan_date.weekday() == 0,
    'FRIDAY': plan_date.weekday() == 4,
    'MONTH_START': plan_date.day <= 3,
  }
  return sorted(k for k, v in fired.items() if v)


# -- the pick ----------------------------------------------------------

def _render(n, text, live_numbers):
  """body (+ live_line) with placeholders filled; never raises."""
  try:
    body = n['body'].format_map(text)
  except (KeyError, IndexError, ValueError):
    body = n['body']
  live_used = False
  live = (n.get('live_line') or '').strip()
  if live and live_numbers:
    try:
      body = body + ' ' + live.format_map(text)
      live_used = True
    except (KeyError, IndexError, ValueError):
      pass
  return body, live_used


def _logged(cursor, plan_date, channel):
  cursor.execute("""
      SELECT TOP 1 log_id, plan_date, channel, nugget_id, concept_code, concept_name,
             audience, seq, picked_reason, text_sent, picked_at, picked_by
      FROM dbo.vw_pc_teaching_nugget_log
      WHERE plan_date = %s AND channel = %s
  """, (plan_date, channel))
  return cursor.fetchone()


def _row_out(r, status='success', already=True):
  return {
    'status': status, 'already_logged': already, 'version': _NUGGET_VERSION,
    'plan_date': make_serializable(r['plan_date']), 'channel': r['channel'],
    'nugget_id': r['nugget_id'], 'concept_code': r['concept_code'],
    'concept_name': r['concept_name'], 'audience': r['audience'], 'seq': r['seq'],
    'reason': r['picked_reason'], 'text': r['text_sent'],
    'picked_at': make_serializable(r['picked_at']), 'picked_by': r['picked_by'],
  }


def _pick_impl(cursor, plan_date, channel, run, p, np, actor):
  existing = _logged(cursor, plan_date, channel)
  if existing:
    return _row_out(existing)

  cursor.execute("""
      SELECT n.nugget_id, n.concept_code, n.concept_name, n.audience, n.seq,
             n.trigger_code, n.trigger_priority, n.body, n.live_line,
             l.last_shown
      FROM dbo.pc_teaching_nugget n
      LEFT JOIN (SELECT nugget_id, MAX(plan_date) AS last_shown
                 FROM dbo.pc_teaching_nugget_log WHERE channel = %s
                 GROUP BY nugget_id) l ON l.nugget_id = n.nugget_id
      WHERE n.is_active = 1
      ORDER BY n.seq, n.nugget_id
  """, (channel,))
  pool = [dict(r) for r in cursor.fetchall()]
  pool = [n for n in pool if n['audience'] in _AUDIENCE[channel]]
  if not pool:
    return {'status': 'skipped', 'channel': channel, 'plan_date': plan_date.isoformat(),
            'message': 'no active lessons for this channel'}
  for n in pool:
    ls = n.get('last_shown')
    n['last_shown'] = ls if (ls is None or isinstance(ls, date)) else _parse_date(ls)

  # yesterday's reason on this channel: trigger days alternate with curriculum
  cursor.execute("""
      SELECT TOP 1 picked_reason FROM dbo.pc_teaching_nugget_log
      WHERE channel = %s AND plan_date < %s ORDER BY plan_date DESC
  """, (channel, plan_date))
  r = cursor.fetchone()
  last_reason = (r['picked_reason'] if r else '') or ''
  after_trigger_day = last_reason.startswith('trigger:')

  text, raw = _numbers(cursor, plan_date, run, p, np)
  fired = _triggers(raw, plan_date, np['thresholds'])
  cutoff = _minus_business_days(plan_date, np['repeat_days'])

  def outside_window(n):
    return n['last_shown'] is None or n['last_shown'] <= cutoff

  pick, reason = None, None
  # 1. foundations, in order, before anything else
  found = [n for n in pool if n['seq'] <= np['foundation_seq'] and n['last_shown'] is None]
  if found:
    pick, reason = found[0], 'foundation'
  # 2. a fired trigger
  if pick is None and fired:
    cands = [n for n in pool if n['trigger_code'] in fired and outside_window(n)
             and (not after_trigger_day or n['trigger_priority'] >= _TRIGGER_TWO_DAYS_MIN_PRIORITY)]
    if cands:
      cands.sort(key=lambda n: (-int(n['trigger_priority'] or 0), n['seq']))
      pick, reason = cands[0], f"trigger:{cands[0]['trigger_code']}"
  # 3. curriculum
  if pick is None:
    unseen = [n for n in pool if n['last_shown'] is None]
    if unseen:
      pick, reason = unseen[0], 'curriculum'
  # 4. repeat, least recently shown
  if pick is None:
    pool.sort(key=lambda n: (n['last_shown'], n['seq']))
    pick, reason = pool[0], 'repeat'

  body, live_used = _render(pick, text, np['live_numbers'])
  context = json.dumps({'fired': fired, 'after_trigger_day': after_trigger_day,
                        'numbers': {k: v for k, v in text.items()}}, default=str)[:2000]
  cursor.execute("""
      INSERT INTO dbo.pc_teaching_nugget_log
          (plan_date, channel, nugget_id, picked_reason, text_sent, trigger_context, picked_by)
      VALUES (%s, %s, %s, %s, %s, %s, %s)
  """, (plan_date, channel, pick['nugget_id'], reason[:60], body[:1000], context,
        (actor or 'agent')[:50]))
  out = {
    'status': 'success', 'already_logged': False, 'version': _NUGGET_VERSION,
    'plan_date': plan_date.isoformat(), 'channel': channel,
    'nugget_id': pick['nugget_id'], 'concept_code': pick['concept_code'],
    'concept_name': pick['concept_name'], 'audience': pick['audience'], 'seq': pick['seq'],
    'reason': reason, 'text': body, 'live_used': live_used, 'fired': fired,
  }
  return out


def _pick_run(plan_date=None, channels=None, run=None, actor=None):
  plan_date = _parse_date(plan_date) if plan_date else date.today()
  channels = [c for c in (channels or list(_CHANNELS)) if c in _CHANNELS] or list(_CHANNELS)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if not _table_ready(cursor):
      return {'status': 'error', 'message': 'pc_teaching_nugget missing - apply migration 073 first.', 'picks': []}
    p = _get_params(cursor)
    np = _nugget_params(p)
    if not np['enabled']:
      return {'status': 'skipped', 'message': 'NUGGET_ENABLED is 0', 'picks': []}
    picks = []
    for ch in channels:
      picks.append(_pick_impl(cursor, plan_date, ch, run, p, np, actor))
    conn.commit()
    return {'status': 'success', 'plan_date': plan_date.isoformat(), 'picks': picks,
            'version': _NUGGET_VERSION}
  except Exception:
    try:
      conn.rollback()
    except Exception:
      pass
    raise
  finally:
    conn.close()


@anvil.server.callable
def nugget_pick(plan_date=None, channel=None, run=None):
  """Pick (or return) the day's lesson. channel None = both channels.
  From the app's server console: anvil.server.call('nugget_pick')"""
  return _pick_run(plan_date, [channel] if channel else None, run, actor='console')


@anvil.server.callable
def nugget_today(channel='MEETING', plan_date=None):
  """The lesson logged for the day (or the latest before it) on a channel,
  for a label on a form. None when nothing is logged yet."""
  plan_date = _parse_date(plan_date) if plan_date else date.today()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if not _table_ready(cursor):
      return None
    cursor.execute("""
        SELECT TOP 1 log_id, plan_date, channel, nugget_id, concept_code, concept_name,
               audience, seq, picked_reason, text_sent, picked_at, picked_by
        FROM dbo.vw_pc_teaching_nugget_log
        WHERE channel = %s AND plan_date <= %s
        ORDER BY plan_date DESC
    """, (channel, plan_date))
    r = cursor.fetchone()
    return _row_out(r) if r else None
  finally:
    _release(conn)


# -- the week, for the Friday digest -----------------------------------

def _week_impl(cursor, end_date):
  mon, fri = _week_of(end_date)
  cursor.execute("""
      SELECT plan_date, weekday, channel, concept_code, concept_name, audience,
             picked_reason, trigger_context
      FROM dbo.vw_pc_teaching_nugget_log
      WHERE plan_date BETWEEN %s AND %s
      ORDER BY plan_date, channel
  """, (mon, fri))
  rows = [dict(r) for r in cursor.fetchall()]
  lessons, fired_all, taught_codes = [], set(), set()
  for r in rows:
    if r['channel'] == 'MEETING':
      taught_codes.add(r['concept_code'])
    try:
      ctx = json.loads(r.get('trigger_context') or '{}')
      fired_all.update(ctx.get('fired') or [])
    except Exception:
      pass
    lessons.append({'plan_date': make_serializable(r['plan_date']), 'weekday': r['weekday'],
                    'channel': r['channel'], 'concept_code': r['concept_code'],
                    'concept_name': r['concept_name'], 'audience': r['audience'],
                    'reason': r['picked_reason']})
  # triggers that fired some morning but whose lesson was not the one taught
  cursor.execute("SELECT concept_code, trigger_code FROM dbo.pc_teaching_nugget WHERE is_active = 1")
  by_trigger = {}
  for r in cursor.fetchall():
    if r['trigger_code']:
      by_trigger.setdefault(r['trigger_code'], []).append(r['concept_code'])
  fired_untaught = sorted(t for t in fired_all
                          if t in by_trigger and not (set(by_trigger[t]) & taught_codes))
  # next up on the MEETING channel: unseen, curriculum order
  cursor.execute("""
      SELECT TOP 5 n.seq, n.concept_code, n.concept_name, n.audience
      FROM dbo.pc_teaching_nugget n
      WHERE n.is_active = 1 AND n.audience IN ('ALL','PC','SALES','OFFICE')
        AND NOT EXISTS (SELECT 1 FROM dbo.pc_teaching_nugget_log l
                        WHERE l.nugget_id = n.nugget_id AND l.channel = 'MEETING')
      ORDER BY n.seq
  """)
  next_up = [dict(r) for r in cursor.fetchall()]
  # (SQL Server refuses a subquery inside an aggregate - error 130 - so
  # three scalar subqueries, not one SUM(CASE WHEN EXISTS ...))
  cursor.execute("""
      SELECT (SELECT COUNT(*) FROM dbo.pc_teaching_nugget WHERE is_active = 1) AS active,
             (SELECT COUNT(DISTINCT l.nugget_id) FROM dbo.pc_teaching_nugget_log l
              JOIN dbo.pc_teaching_nugget n ON n.nugget_id = l.nugget_id
              WHERE n.is_active = 1 AND l.channel = 'MEETING') AS taught_meeting,
             (SELECT COUNT(DISTINCT l.nugget_id) FROM dbo.pc_teaching_nugget_log l
              JOIN dbo.pc_teaching_nugget n ON n.nugget_id = l.nugget_id
              WHERE n.is_active = 1 AND l.channel = 'ASSEMBLY') AS taught_assembly
  """)
  c = cursor.fetchone()
  # rule candidates the Monday scan proposed and nobody has decided yet -
  # a standing line in the digest so a skipped proposal keeps resurfacing
  pending_rules = None
  if _obj_exists(cursor, 'pc_plan_rule_register'):
    cursor.execute("SELECT COUNT(*) AS n FROM dbo.pc_plan_rule_register "
                   "WHERE status IN ('PROPOSED','NEEDS_BUILD')")
    pending_rules = int(cursor.fetchone()['n'] or 0)
  return {
    'status': 'success', 'version': _NUGGET_VERSION,
    'week_start': mon.isoformat(), 'week_end': fri.isoformat(),
    'lessons': lessons, 'fired_untaught': fired_untaught, 'next_up': next_up,
    'counts': {'active': int(c['active'] or 0), 'taught_meeting': int(c['taught_meeting'] or 0),
               'taught_assembly': int(c['taught_assembly'] or 0)},
    'pending_rules': pending_rules,
  }


@anvil.server.callable
def nugget_week(end_date=None):
  """The week's lessons (Mon-Fri of the week holding end_date), the
  triggers that fired but were not taught, and the next five in line."""
  end_date = _parse_date(end_date) if end_date else date.today()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if not _table_ready(cursor):
      return {'status': 'error', 'message': 'pc_teaching_nugget missing - apply migration 073 first.'}
    return _week_impl(cursor, end_date)
  finally:
    _release(conn)


# -- the library, for an editor form (Dave builds forms by hand) -------

@anvil.server.callable
def nugget_list(include_inactive=False):
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if not _table_ready(cursor):
      return []
    cursor.execute(f"""
        SELECT n.nugget_id, n.concept_code, n.concept_name, n.audience, n.seq,
               n.trigger_code, n.trigger_priority, n.related_flag, n.body, n.live_line,
               n.source, n.is_active, n.updated_at, n.updated_by,
               (SELECT MAX(plan_date) FROM dbo.pc_teaching_nugget_log l
                WHERE l.nugget_id = n.nugget_id AND l.channel = 'MEETING') AS last_meeting,
               (SELECT COUNT(*) FROM dbo.pc_teaching_nugget_log l
                WHERE l.nugget_id = n.nugget_id) AS times_shown
        FROM dbo.pc_teaching_nugget n
        {'' if include_inactive else 'WHERE n.is_active = 1'}
        ORDER BY n.seq, n.nugget_id
    """)
    return [{k: make_serializable(v) for k, v in r.items()} for r in cursor.fetchall()]
  finally:
    _release(conn)


@anvil.server.callable
def nugget_save(row, actor=None):
  """Insert or update one lesson by concept_code. row keys: concept_code
  (required), concept_name, audience, seq, trigger_code, trigger_priority,
  related_flag, body, live_line, source, is_active."""
  code = (row or {}).get('concept_code', '').strip().upper()
  if not code:
    return {'status': 'error', 'message': 'concept_code is required'}
  cols = ('concept_name', 'audience', 'seq', 'trigger_code', 'trigger_priority',
          'related_flag', 'body', 'live_line', 'source', 'is_active')
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("SELECT nugget_id FROM dbo.pc_teaching_nugget WHERE concept_code = %s", (code,))
    r = cursor.fetchone()
    if r:
      sets = [f"{c} = %s" for c in cols if c in row]
      vals = [row[c] for c in cols if c in row]
      if sets:
        cursor.execute(f"UPDATE dbo.pc_teaching_nugget SET {', '.join(sets)}, "
                       f"updated_at = SYSDATETIME(), updated_by = %s WHERE nugget_id = %s",
                       (*vals, (actor or 'app')[:50], r['nugget_id']))
      nugget_id = r['nugget_id']
    else:
      for req in ('concept_name', 'seq', 'body'):
        if not row.get(req):
          return {'status': 'error', 'message': f'{req} is required for a new lesson'}
      cursor.execute("""
          INSERT INTO dbo.pc_teaching_nugget
              (concept_code, concept_name, audience, seq, trigger_code, trigger_priority,
               related_flag, body, live_line, source, is_active, created_by)
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
      """, (code, row['concept_name'], row.get('audience') or 'ALL', int(row['seq']),
            row.get('trigger_code') or None, int(row.get('trigger_priority') or 50),
            row.get('related_flag') or None, row['body'], row.get('live_line') or None,
            row.get('source') or None, 1 if row.get('is_active', True) else 0,
            (actor or 'app')[:50]))
      cursor.execute("SELECT nugget_id FROM dbo.pc_teaching_nugget WHERE concept_code = %s", (code,))
      nugget_id = cursor.fetchone()['nugget_id']
    conn.commit()
    return {'status': 'success', 'nugget_id': nugget_id, 'concept_code': code}
  except Exception:
    try:
      conn.rollback()
    except Exception:
      pass
    raise
  finally:
    conn.close()


# -- the service endpoint ----------------------------------------------

@anvil.server.http_endpoint('/agent/nugget', methods=['POST'])
def agent_nugget_endpoint(**kwargs):
  """titan-ai-service: {"action": "pick", "plan_date": ..., "channels": [...],
  "run": {...}} during the morning run; {"action": "digest", "end_date": ...}
  for the Friday text. Synchronous, a handful of queries."""
  if not _agent_key_ok():
    return anvil.server.HttpResponse(401, 'unauthorized')
  body = anvil.server.request.body_json or {}
  action = (body.get('action') or 'pick').lower()
  if action == 'digest':
    end_date = _parse_date(body['end_date']) if body.get('end_date') else date.today()
    conn = get_connection()
    try:
      cursor = conn.cursor()
      if not _table_ready(cursor):
        return {'status': 'error', 'message': 'pc_teaching_nugget missing - apply migration 073 first.'}
      return _week_impl(cursor, end_date)
    finally:
      _release(conn)
  return _pick_run(body.get('plan_date'), body.get('channels'), body.get('run') or {}, actor='agent')