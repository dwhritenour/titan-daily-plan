import anvil.server
from datetime import date, datetime, timedelta

from .daily_ship_plan import (get_connection, _parse_date, _get_params,
make_serializable, _agent_key_ok)
from .daily_plan_shipped import orders_shipped_get_day
try:
  from .daily_plan_completed import wo_completed_get_day
except Exception:                       # module not pasted yet: done_inches stays NULL
  wo_completed_get_day = None

# =============================================================================
# CAPACITY LEDGER  (ledger-1.0.0, 2026-09-16)
#
# Dave: "if the capacity number was wrong ... faith in this project is
# lost." So the model is judged in public, every day, on one line:
#
#   estimate   the agent's 06:30 capacity (pc_agent_proposal - full roster)
#   published  the number stamped at publish (pc_ship_plan - 08:45 attendance)
#   actual     credited attendance for the day (067 credit view, the SAME
#              formula the retro uses for crew_actual / capacity_actual)
#   planned    Must+Next work-inches at publish (snapshot rev 0) and as the
#              day ended (the line table)
#   shipped    work-inches that left the building per e21 - the SAME totals
#              the orders_shipped form shows (orders_shipped_get_day)
#   done       work orders done that day - the SAME totals the wo_completed
#              form shows (wo_completed_get_day)
#
# Derived: daily_ratio = shipped / actual capacity. Shipping trails building
# by up to a day, so the band (LEDGER_TOLERANCE_PCT) is judged on the
# rolling ratio over LEDGER_WINDOW_DAYS ledger days, and only once the
# window holds 3 days. implied_efficiency = the EFFICIENCY_FACTOR that would
# have made capacity equal what shipped - what the monthly re-seed reads.
#
# Runs: titan-ai-service POSTs /_/api/agent/ledger at LEDGER_RUN_TIME
# (08:45, after the ~08:17 sales_orders_ship load) for the previous business
# day and re-computes LEDGER_DAYS_BACK days behind it (late ship rows, late
# attendance). Idempotent: every run upserts. Days with no Published plan
# are skipped. Nothing here writes anywhere but pc_capacity_ledger.
# Migration 069. Shadow mode: the text goes to DAILY_PLAN_LEDGER (Dave).
# =============================================================================

_LEDGER_VERSION = 'ledger-1.0.0'
_ACTIVE_PLAN = ('Planned', 'Confirmed', 'Proposed')


def _prev_business_day(d=None):
  d = (d or date.today()) - timedelta(days=1)
  while d.weekday() >= 5:
    d -= timedelta(days=1)
  return d


def _back_business_days(end, n):
  """[end, end-1bd, ... ] - n business days ending at end, oldest first."""
  out, d = [], end
  while len(out) < max(1, n):
    if d.weekday() < 5:
      out.append(d)
    d -= timedelta(days=1)
  return list(reversed(out))


def _f(v, default=None):
  try:
    return float(v)
  except (TypeError, ValueError):
    return default


def _r1(v):
  return None if v is None else round(float(v), 1)


def _ledger_params(p):
  def _num(k, d):
    try:
      return float(p.get(k, d))
    except (TypeError, ValueError):
      return float(d)
  return {'enabled': str(p.get('LEDGER_ENABLED', '1')).strip() == '1',
          'tolerance_pct': max(0.0, _num('LEDGER_TOLERANCE_PCT', 10)),
          'window_days': max(1, int(_num('LEDGER_WINDOW_DAYS', 5))),
          'days_back': max(1, int(_num('LEDGER_DAYS_BACK', 3))),
          'fill_pct': max(0.0, min(_num('AGENT_FILL_PCT', 80), 100.0)),
          'efficiency': _num('EFFICIENCY_FACTOR', 1.2) or 1.0,
          'hours': _num('HOURS_PER_DAY', 8.0)}


def _table_ready(cursor):
  cursor.execute("SELECT OBJECT_ID('dbo.pc_capacity_ledger') AS o")
  return (cursor.fetchone() or {}).get('o') is not None


def _parse_crew(detail):
  """'crew 14 eff x 8h / 1.2 - base (snapshot 2026-09-15)' -> 14.0"""
  try:
    txt = (detail or '').lower()
    if 'crew' not in txt:
      return None
    tail = txt.split('crew', 1)[1].strip()
    num = ''
    for ch in tail:
      if ch.isdigit() or ch == '.':
        num += ch
      else:
        break
    return float(num) if num else None
  except Exception:
    return None


def _actual_capacity(cursor, plan_date, lp):
  """The day's credited crew and the capacity it implies - the retro's
  (067) formula, recomputed here so the ledger does not depend on the
  retro having run. Returns (crew, capacity, basis)."""
  cursor.execute("""
      SELECT SUM(COALESCE(o.capacity_factor,
                          r.default_capacity_factor * ISNULL(c.credit, 0),
                          ISNULL(c.credit, 0))) AS crew,
             SUM(CASE WHEN c.employee_id IS NULL THEN 0 ELSE 1 END) AS credited
      FROM dbo.em_assembler_roster r
      LEFT JOIN dbo.vw_em_assembler_credit c
             ON c.employee_id = r.employee_id AND c.status_date = %s
      LEFT JOIN dbo.pc_assembler_day_override o
             ON o.employee_id = r.employee_id AND o.override_date = %s
      WHERE r.end_date IS NULL
        AND (r.start_date IS NULL OR r.start_date <= %s)
  """, (plan_date, plan_date, plan_date))
  row = cursor.fetchone() or {}
  crew = _f(row.get('crew'), 0.0) or 0.0
  credited = int(row.get('credited') or 0)
  cursor.execute("""
      SELECT MAX(CASE WHEN pw.factor_code = 'ST_STD' THEN pw.factor_value END) AS st_std,
             MAX(CASE WHEN pw.factor_code = 'BASE_OVERHEAD' THEN pw.factor_value END) AS base_hrs
      FROM dbo.pc_weight_set ws JOIN dbo.pc_weight pw ON pw.weight_set_id = ws.weight_set_id
      WHERE ws.retired_date IS NULL
  """)
  w = cursor.fetchone() or {}
  st_std = _f(w.get('st_std'), 0.0) or 0.0
  base = _f(w.get('base_hrs'), 0.0) or 0.0
  if credited == 0 or not st_std:
    return None, None, 'published'          # no attendance rows for that day
  cap = round(crew * (lp['hours'] * 60.0 - base * 60.0 / 5.0) / lp['efficiency'] / st_std, 1)
  return round(crew, 2), cap, 'attendance'


def _ledger_day(cursor, plan_date, lp, actor):
  """Compute and upsert one day. Returns the row dict, or None when the
  day has no Published plan."""
  cursor.execute("""
      SELECT plan_status, published_at, revision_no, capacity_inches, capacity_detail
      FROM dbo.pc_ship_plan WHERE plan_date = %s
  """, (plan_date,))
  hdr = cursor.fetchone()
  if not hdr or hdr.get('plan_status') != 'Published':
    return None
  notes = []

  # --- the estimate: the agent's latest proposal for the day ------------
  cursor.execute("""
      SELECT TOP 1 capacity_inches, generated_at FROM dbo.pc_agent_proposal
      WHERE plan_date = %s ORDER BY proposal_id DESC
  """, (plan_date,))
  ap = cursor.fetchone() or {}
  est_cap = _f(ap.get('capacity_inches'))
  pub_cap = _f(hdr.get('capacity_inches'))
  pub_crew = _parse_crew(hdr.get('capacity_detail'))

  # --- the actual: credited attendance ----------------------------------
  act_crew, act_cap, basis = _actual_capacity(cursor, plan_date, lp)
  if act_cap is None:
    act_cap, act_crew = pub_cap, pub_crew
    notes.append('no attendance credit for the day: actual = published')

  # --- the plan: at publish (snapshot rev 0) and as the day ended --------
  cursor.execute("""
      SELECT SUM(CASE WHEN was_planned = 1 AND ISNULL(tier, '') <> 'Stretch' THEN load_inches ELSE 0 END) AS planned,
             SUM(CASE WHEN was_planned = 1 AND tier = 'Stretch' THEN load_inches ELSE 0 END) AS stretch
      FROM dbo.pc_plan_flag_snapshot WHERE plan_date = %s AND revision_no = 0
  """, (plan_date,))
  s0 = cursor.fetchone() or {}
  cursor.execute("""
      SELECT SUM(CASE WHEN tier <> 'Stretch' THEN load_inches ELSE 0 END) AS planned,
             SUM(CASE WHEN tier = 'Stretch' THEN load_inches ELSE 0 END) AS stretch,
             COUNT(*) AS n_lines
      FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND line_status IN ('Planned', 'Confirmed', 'Proposed')
  """, (plan_date,))
  sf = cursor.fetchone() or {}
  planned_pub = _f(s0.get('planned'), 0.0) or 0.0
  planned_fin = _f(sf.get('planned'), 0.0) or 0.0

  # --- what finished: the two forms' own totals --------------------------
  shipped = shipped_planned = shipped_unplanned = None
  n_ship = n_zero = n_noest = None
  feed_loaded = False
  try:
    sh = orders_shipped_get_day(plan_date)
    if sh.get('status') == 'success':
      t = sh.get('totals') or {}
      if int(t.get('n') or 0) > 0:
        feed_loaded = True
        shipped = _f(t.get('actual_inches'), 0.0)
        shipped_planned = _f(t.get('actual_planned_inches'), 0.0)
        shipped_unplanned = _f(t.get('actual_unplanned_inches'), 0.0)
        n_ship, n_zero, n_noest = (int(t.get('n') or 0), int(t.get('n_zero') or 0),
                                   int(t.get('n_no_estimate') or 0))
      else:
        notes.append('sales_orders_ship has no rows for the day yet')
    else:
      notes.append(f"shipped: {sh.get('message', 'error')}")
  except Exception as e:
    notes.append(f'shipped: {e}'[:120])
  done = None
  if wo_completed_get_day is not None:
    try:
      dn = wo_completed_get_day(plan_date)
      if dn.get('status') == 'success':
        done = _f((dn.get('totals') or {}).get('actual_inches'), 0.0)
    except Exception as e:
      notes.append(f'done: {e}'[:120])

  # --- keep yesterday's shipped figures if the feed rolled off -----------
  if not feed_loaded:
    cursor.execute("""
        SELECT shipped_inches, shipped_planned_inches, shipped_unplanned_inches,
               n_shipped_lines, n_shipped_zero, n_shipped_no_estimate, ship_feed_loaded
        FROM dbo.pc_capacity_ledger WHERE plan_date = %s
    """, (plan_date,))
    old = cursor.fetchone()
    if old and old.get('ship_feed_loaded'):
      shipped, shipped_planned, shipped_unplanned = (_f(old['shipped_inches']),
                                                     _f(old['shipped_planned_inches']),
                                                     _f(old['shipped_unplanned_inches']))
      n_ship, n_zero, n_noest = old['n_shipped_lines'], old['n_shipped_zero'], old['n_shipped_no_estimate']
      feed_loaded = True
      notes.append('shipped figures kept from an earlier run (feed rolled off)')

  # --- derived --------------------------------------------------------------
  fill_target = round(pub_cap * lp['fill_pct'] / 100.0, 1) if pub_cap else None
  daily_ratio = (round(shipped / act_cap, 3) if (shipped is not None and act_cap) else None)
  attainment = (round(shipped_planned / planned_fin * 100.0, 1)
                if (shipped_planned is not None and planned_fin) else None)
  attendance = round(pub_cap / est_cap * 100.0, 1) if (pub_cap and est_cap) else None
  plan_load = round(planned_fin / act_cap * 100.0, 1) if (act_cap and planned_fin) else None
  implied_eff = (round(lp['efficiency'] * act_cap / shipped, 3)
                 if (shipped and act_cap) else None)
  is_final = 1 if (feed_loaded and plan_date < date.today()) else 0

  row = {
    'plan_date': plan_date, 'ledger_version': _LEDGER_VERSION, 'is_final': is_final,
    'est_capacity_inches': _r1(est_cap), 'est_generated_at': ap.get('generated_at'),
    'published_capacity_inches': _r1(pub_cap), 'published_crew': pub_crew,
    'published_at': hdr.get('published_at'),
    'actual_capacity_inches': _r1(act_cap), 'actual_crew': act_crew, 'actual_basis': basis,
    'fill_pct': lp['fill_pct'], 'fill_target_inches': fill_target,
    'planned_publish_inches': _r1(planned_pub), 'stretch_publish_inches': _r1(_f(s0.get('stretch'), 0.0)),
    'planned_final_inches': _r1(planned_fin), 'stretch_final_inches': _r1(_f(sf.get('stretch'), 0.0)),
    'n_plan_lines': int(sf.get('n_lines') or 0), 'revision_no': hdr.get('revision_no'),
    'shipped_inches': _r1(shipped), 'shipped_planned_inches': _r1(shipped_planned),
    'shipped_unplanned_inches': _r1(shipped_unplanned),
    'n_shipped_lines': n_ship, 'n_shipped_zero': n_zero, 'n_shipped_no_estimate': n_noest,
    'done_inches': _r1(done), 'ship_feed_loaded': 1 if feed_loaded else 0,
    'daily_ratio': daily_ratio, 'attainment_pct': attainment, 'attendance_pct': attendance,
    'plan_load_pct': plan_load, 'efficiency_used': round(lp['efficiency'], 3),
    'implied_efficiency': implied_eff, 'tolerance_pct': lp['tolerance_pct'],
    'notes': ('; '.join(notes))[:500] or None,
  }
  cols = [k for k in row.keys()]
  cursor.execute("SELECT 1 AS x FROM dbo.pc_capacity_ledger WHERE plan_date = %s", (plan_date,))
  if cursor.fetchone():
    sets = ', '.join(f'{c} = %s' for c in cols if c != 'plan_date')
    cursor.execute(f"UPDATE dbo.pc_capacity_ledger SET {sets}, run_at = SYSDATETIME() "
                   f"WHERE plan_date = %s",
                   tuple(row[c] for c in cols if c != 'plan_date') + (plan_date,))
  else:
    cursor.execute(f"INSERT INTO dbo.pc_capacity_ledger ({', '.join(cols)}) "
                   f"VALUES ({', '.join(['%s'] * len(cols))})",
                   tuple(row[c] for c in cols))
  return row


def _roll(cursor, plan_date, lp):
  """Rolling ratio over the last window_days ledger days ending at
  plan_date (days with shipped figures only); within_band once the window
  holds 3 days. Updates the row; returns (ratio, days, within)."""
  cursor.execute("""
      SELECT TOP (%s) shipped_inches, actual_capacity_inches
      FROM dbo.pc_capacity_ledger
      WHERE plan_date <= %s AND shipped_inches IS NOT NULL AND actual_capacity_inches > 0
      ORDER BY plan_date DESC
  """, (lp['window_days'], plan_date))
  rows = cursor.fetchall()
  n = len(rows)
  ratio = within = None
  if n:
    num = sum(float(r['shipped_inches']) for r in rows)
    den = sum(float(r['actual_capacity_inches']) for r in rows)
    ratio = round(num / den, 3) if den else None
    if ratio is not None and n >= 3:
      within = 1 if abs(ratio - 1.0) * 100.0 <= lp['tolerance_pct'] else 0
  cursor.execute("""
      UPDATE dbo.pc_capacity_ledger
      SET rolling_ratio = %s, rolling_days = %s, within_band = %s
      WHERE plan_date = %s
  """, (ratio, n, within, plan_date))
  return ratio, n, within


def _ledger_run_impl(plan_date=None, days_back=None, actor=None):
  """Compute the ledger for plan_date (default: previous business day)
  and the LEDGER_DAYS_BACK business days before it. Never raises."""
  end = _parse_date(plan_date) if plan_date else _prev_business_day()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if not _table_ready(cursor):
      return {'status': 'error', 'message': 'pc_capacity_ledger missing - apply migration 069 first.'}
    lp = _ledger_params(_get_params(cursor))
    if not lp['enabled']:
      return {'status': 'skipped', 'message': 'LEDGER_ENABLED is 0.'}
    n_back = int(days_back) if days_back else lp['days_back']
    days = _back_business_days(end, n_back)
    out, skipped = [], []
    for d in days:
      row = _ledger_day(cursor, d, lp, actor)
      if row is None:
        skipped.append(d.isoformat())
        continue
      ratio, n, within = _roll(cursor, d, lp)
      row.update({'rolling_ratio': ratio, 'rolling_days': n, 'within_band': within})
      out.append({k: make_serializable(v) for k, v in row.items()})
    conn.commit()
    return {'status': 'success', 'version': _LEDGER_VERSION, 'end_date': end.isoformat(),
            'rows': out, 'skipped': skipped, 'tolerance_pct': lp['tolerance_pct'],
            'window_days': lp['window_days']}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Ledger run failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ledger_run(plan_date=None, days_back=None):
  """Manual run from the app / server console: ledger_run('2026-09-15', 3)."""
  return _ledger_run_impl(plan_date, days_back, actor='app')


@anvil.server.http_endpoint('/agent/ledger', methods=['POST'])
def agent_ledger_endpoint(**kwargs):
  """titan-ai-service calls this at LEDGER_RUN_TIME. Synchronous - a few
  queries per day, LEDGER_DAYS_BACK days."""
  if not _agent_key_ok():
    return anvil.server.HttpResponse(401, 'unauthorized')
  body = anvil.server.request.body_json or {}
  return _ledger_run_impl(body.get('plan_date'), body.get('days_back'), actor='agent')


@anvil.server.callable
def ledger_get_rows(limit=30):
  """The ledger as people read it, newest first (vw_pc_capacity_ledger)."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if not _table_ready(cursor):
      return {'status': 'error', 'message': 'pc_capacity_ledger missing - apply migration 069 first.', 'rows': []}
    cursor.execute(f"SELECT TOP ({int(limit)}) * FROM dbo.vw_pc_capacity_ledger ORDER BY plan_date DESC")
    rows = [{k: make_serializable(v) for k, v in r.items()} for r in cursor.fetchall()]
    return {'status': 'success', 'rows': rows}
  finally:
    conn.close()