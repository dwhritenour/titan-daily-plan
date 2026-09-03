import anvil.secrets
"""
Anvil Server Module - Daily Ship Plan
======================================
Server-side code for the Ship Plan form (Week-2 release of the Daily
Ship Plan ladder — DECISION, exchange topic 2026-08-13_daily-ship-plan).

Spec: documents/Ship_Plan_Form_Specification.docx v1.0.0
Schema: migrations 027 (reference/config) + 028 (plan core), applied
2026-08-19. The client computes NOTHING — all load math lives in
vw_pc_ship_candidate_rows / vw_pc_ship_candidates, all capacity math in
vw_pc_capacity_day (today) or _capacity_for() (future-date estimate).

Governing rules enforced HERE, not in the client:
    - Every mutation writes its pc_plan_line_events row INSIDE the same
      transaction. A change without its event is impossible.
    - DRIFT RULE (Dave, 2026-08-19): plan lines never silently re-total.
      get_plan() only REPORTS drift; recalc_line() is the only re-total
      path and it logs Recalc with old/new.
    - Cap hit is the CLIENT's dialog, but the suggested day comes from
      _suggest_day() here. Nothing is ever dropped silently.
    - Nothing is deleted: remove_line() sets status 'Removed'.
    - Wk-3 verbs (Proposed/Confirmed) exist in the schema CHECKs and in
      _ACTIVE_STATUSES but have no callables yet — propose_line() and
      confirm_line() arrive with the Wk-3 release, nothing else changes.

NOTE on Change Log: PD's ask was one cl_change_log entry per LADDER
RELEASE (a deploy), not per morning publish — so publish_plan() does not
write to the Change Log app's tables. The Wk-2 release itself gets its
entry at deploy time, in that app.

Tables read : vw_pc_ship_candidates, vw_pc_ship_candidate_rows,
              vw_pc_capacity_day, pc_plan_parameters, pc_weight_set,
              pc_weight, pc_special_customers, em_assembler_roster,
              em_assembler_status_daily, pc_assembler_day_override,
              sales_orders_due (drift compare only)
Tables written: pc_ship_plan, pc_ship_plan_line, pc_ship_plan_line_source,
              pc_plan_line_events, pc_plan_parameters,
              pc_assembler_day_override, pc_special_customers

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: August 2026
"""

import anvil.server
import threading
import os
import time
from datetime import datetime, date, timedelta
from decimal import Decimal

from . import db_config


# =============================================================================
# HELPERS (house pattern — see part_master.py)
# =============================================================================

_ACTIVE_STATUSES = ('Planned', 'Snoozed', 'Proposed', 'Confirmed')
_COUNTS_AGAINST_CAP = ('Planned', 'Proposed', 'Confirmed')   # Snoozed moved away


def make_serializable(value):
  if value is None:
    return None
  elif isinstance(value, (datetime, date)):
    return value.isoformat()
  elif isinstance(value, Decimal):
    return float(value)
  elif isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  else:
    return value


def serialize_row(row):
  return {key: make_serializable(val) for key, val in row.items()}


# ---------------------------------------------------------------------
# Connection pool + cached secrets (perf, 2026-08-26)
#
# MEASURED from the running app: WS transport is ~100ms, but every call
# cost ~900ms — the rest was 4 anvil.secrets fetches + a fresh WAN login
# to SQL Server, paid per call because connections were opened/closed
# per call. A thread-LOCAL cache didn't help (each call runs on a fresh
# thread), so this is a real pool: module-level, lock-guarded, any
# thread can reuse a parked connection. Module state only survives if
# the process does — ship_plan_debug_stats() proves whether it does.
# ---------------------------------------------------------------------
_process_t0 = time.time()
_stats = {'gets': 0, 'pool_hits': 0, 'pool_misses': 0, 'connects': 0}
_secret_cache = {}
_pool = []                       # parked, clean connections
_pool_lock = threading.Lock()
_POOL_MAX = 4


def _secret(name):
  """anvil.secrets.get_secret is a platform RPC — fetch each secret once
  per process, not four times per call."""
  if name not in _secret_cache:
    _secret_cache[name] = anvil.secrets.get_secret(name)
  return _secret_cache[name]


def _connect():
  import pymssql
  _stats['connects'] += 1
  _t = time.time()
  conn = pymssql.connect(
    server=_secret('NEXUS_SERVER'),
    user=_secret('NEXUS_USERNAME'),
    password=_secret('NEXUS_PASSWORD'),
    database=_secret('NEXUS_DATABASE'),
    port='1433',
    as_dict=True)
  _stats['last_connect_ms'] = int((time.time() - _t) * 1000)
  return conn


class _PooledConnection:
  """Wraps a pooled pymssql connection so existing call sites work
  UNCHANGED: close() parks the connection back in the pool instead of
  closing the socket. Every call path already ends in commit-or-rollback
  before close, so parked connections are clean."""
  __slots__ = ('_real',)

  def __init__(self, real):
    self._real = real

  def cursor(self):
    return self._real.cursor()

  def commit(self):
    self._real.commit()

  def rollback(self):
    self._real.rollback()

  def close(self):
    real, self._real = self._real, None
    if real is None:
      return
    with _pool_lock:
      if len(_pool) < _POOL_MAX:
        _pool.append(real)
        return
    try:
      real.close()
    except Exception:
      pass


def get_connection():
  _stats['gets'] += 1
  while True:
    with _pool_lock:
      real = _pool.pop() if _pool else None
    if real is None:
      break
    try:                          # cheap ping; discard dead connections
      cur = real.cursor()
      # AS ok is load-bearing: as_dict=True raises ColumnsWithoutNames-
      # Error on a nameless column, which silently killed every pooled
      # connection until 2026-08-26's probe caught it.
      cur.execute("SELECT 1 AS ok")
      cur.fetchall()
      real.rollback()             # clear the ping's implicit transaction
      _stats['pool_hits'] += 1
      return _PooledConnection(real)
    except Exception as e:
      _stats['last_ping_err'] = repr(e)[:160]
      try:
        real.close()
      except Exception:
        pass
  _stats['pool_misses'] += 1
  return _PooledConnection(_connect())


@anvil.server.callable
def ship_plan_debug_probe():
  """Times each stage of the DB leg inside ONE call: WAN login, three
  query round trips on the same socket. With last_ping_err from
  cross-call pool attempts, this pins where the ~1s/call actually goes
  and whether parked sockets are killed at call boundaries."""
  out = {'status': 'success', 'pid': os.getpid()}
  t0 = time.time()
  conn = _connect()
  out['connect_ms'] = int((time.time() - t0) * 1000)
  cur = conn.cursor()
  t1 = time.time()
  cur.execute("SELECT 1 AS x")
  cur.fetchall()
  out['query1_ms'] = int((time.time() - t1) * 1000)
  t2 = time.time()
  cur.execute("SELECT COUNT(*) AS n FROM dbo.pc_priority_reasons")
  cur.fetchall()
  out['query2_ms'] = int((time.time() - t2) * 1000)
  t3 = time.time()
  cur.execute("SELECT 1 AS x")
  cur.fetchall()
  out['query3_ms'] = int((time.time() - t3) * 1000)
  try:
    conn.close()
  except Exception:
    pass
  out['stats'] = dict(_stats)
  return out


@anvil.server.callable
def ship_plan_debug_stats():
  """Perf forensics: if pid is stable and process_age_s grows across
  calls, the persistent server is real and the pool can work. If pid
  changes every call, the process is being respawned and no in-process
  cache can help. Harmless to leave in; PC-only info."""
  return {'status': 'success',
          'pid': os.getpid(),
          'process_age_s': round(time.time() - _process_t0, 1),
          'pool_size': len(_pool),
          'secrets_cached': len(_secret_cache),
          'stats': dict(_stats)}


def _current_actor():
  try:
    import anvil.users
    u = anvil.users.get_user()
    if u:
      try:
        return (u['name'] or u['email'] or 'Unknown')[:50]
      except Exception:
        return str(u)[:50]
  except Exception:
    pass
  return 'Production'


def _is_pc_user():
  """Role gate hook. TODO(Dave/Chuck): wire to the app's real role source
  (same pattern the other PC forms use). Until then every logged-in user
  is PC — the CLIENT hides mutating controls for Sales, but the server
  must not rely on that once roles are real."""
  return True


def _parse_date(d):
  """Accept date, datetime, or 'YYYY-MM-DD' string from the client."""
  if isinstance(d, datetime):
    return d.date()
  if isinstance(d, date):
    return d
  return datetime.strptime(str(d)[:10], '%Y-%m-%d').date()


def _log_event(cursor, plan_date, order, sku, event_type,
               old_value=None, new_value=None, actor=None, note=None):
  """Append one pc_plan_line_events row. ALWAYS called inside the same
  transaction as the mutation it records."""
  cursor.execute("""
      INSERT INTO dbo.pc_plan_line_events
          (plan_date, nx_order_numb, nx_sku, event_type,
           old_value, new_value, actor, note)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
  """, (plan_date, order, sku, event_type,
        (str(old_value)[:200] if old_value is not None else None),
        (str(new_value)[:200] if new_value is not None else None),
        (actor or _current_actor()), (note[:500] if note else None)))


def _bump_revision(cursor, plan_date, actor):
  """If the day's plan is already PUBLISHED, count this change as a
  revision — displayed as 'PUBLISHED 8:52 · rev 3'. Draft edits are not
  revisions; the agreement only exists once it's published."""
  cursor.execute("""
      UPDATE dbo.pc_ship_plan
      SET revision_no = revision_no + 1,
          last_revised_at = GETDATE(), last_revised_by = %s
      WHERE plan_date = %s AND plan_status = 'Published'
  """, (actor or _current_actor(), plan_date))


def _get_params(cursor):
  cursor.execute("SELECT param_code, param_value FROM dbo.pc_plan_parameters")
  return {r['param_code']: r['param_value'] for r in cursor.fetchall()}


def _get_active_weights(cursor):
  cursor.execute("""
      SELECT ws.weight_set_id, pw.factor_code, pw.factor_value
      FROM dbo.pc_weight_set ws
      JOIN dbo.pc_weight pw ON pw.weight_set_id = ws.weight_set_id
      WHERE ws.retired_date IS NULL
  """)
  rows = cursor.fetchall()
  if not rows:
    raise Exception('No active weight set (pc_weight_set.retired_date IS NULL)')
  weights = {r['factor_code']: float(r['factor_value']) for r in rows}
  weights['weight_set_id'] = rows[0]['weight_set_id']
  return weights


# =============================================================================
# CAPACITY
# =============================================================================

def _capacity_for(cursor, plan_date):
  """Capacity dict for a date. Today = vw_pc_capacity_day (the 08:45
  snapshot + overrides). Future = same formula with crew estimated as the
  full active roster, adjusted by that date's overrides — is_estimate=True
  so the bar renders hollow."""
  today = date.today()
  if plan_date <= today:
    cursor.execute("SELECT * FROM dbo.vw_pc_capacity_day")
    row = cursor.fetchone()
    if row:
      out = serialize_row(row)
      out['is_estimate'] = False
      out['snapshot_stale'] = (row['snapshot_date'] is not None
                               and row['snapshot_date'] < today)
      out['capacity_detail'] = (
        f"crew {float(row['effective_crew'] or 0):g} eff "
        f"x {float(row['hours_per_day'] or 0):g}h / "
        f"{float(row['efficiency'] or 0):g} - base "
        f"(snapshot {make_serializable(row['snapshot_date'])})")
      return out
    # view empty (no snapshot ever loaded) — fall through to estimate

  params = _get_params(cursor)
  weights = _get_active_weights(cursor)
  hours = float(params.get('HOURS_PER_DAY', 8.0))
  eff = float(params.get('EFFICIENCY_FACTOR', 1.20)) or 1.0
  cap_override = params.get('DAILY_CAP_OVERRIDE_IN', '') or None

  cursor.execute("""
      SELECT r.employee_id, r.default_capacity_factor, o.capacity_factor
      FROM dbo.em_assembler_roster r
      LEFT JOIN dbo.pc_assembler_day_override o
             ON o.employee_id = r.employee_id AND o.override_date = %s
      WHERE r.end_date IS NULL
  """, (plan_date,))
  rows = cursor.fetchall()
  roster_count = len(rows)
  overrides = sum(1 for r in rows if r['capacity_factor'] is not None)

  def _credit(r):
    # day override > standing factor (trainee, 034) > full assembler
    if r['capacity_factor'] is not None:
      return float(r['capacity_factor'])
    if r.get('default_capacity_factor') is not None:
      return float(r['default_capacity_factor'])
    return 1.0
  effective_crew = sum(_credit(r) for r in rows)
  trainees = sum(1 for r in rows
                 if r['capacity_factor'] is None
                 and r.get('default_capacity_factor') is not None)

  variable_minutes = (effective_crew
                      * (hours * 60.0 - weights['BASE_OVERHEAD'] * 60.0 / 5.0)
                      / eff)
  computed = variable_minutes / weights['ST_STD'] if weights['ST_STD'] else 0
  effective_cap = float(cap_override) if cap_override else computed
  return {
    'capacity_date': plan_date.isoformat(),
    'snapshot_date': None, 'snapshot_stale': False,
    'roster_count': roster_count, 'present_count': None,
    'overrides_applied': overrides,
    'effective_crew': round(effective_crew, 2),
    'hours_per_day': hours, 'efficiency': eff,
    'variable_minutes': round(variable_minutes, 1),
    'capacity_inches_computed': round(computed, 1),
    'capacity_inches_override': float(cap_override) if cap_override else None,
    'capacity_inches_effective': round(effective_cap, 1),
    'is_estimate': True,
    'capacity_detail': (f"ESTIMATE: {effective_crew:g} crew (roster, "
                        f"{overrides} override, {trainees} trainee) "
                        f"x {hours:g}h / {eff:g} - base"),
  }


def _planned_inches(cursor, plan_date):
  """Load counting AGAINST the cap: Must + Next only. Stretch is
  'if capacity permits' by definition — it never trips the over-cap
  check (Dave's tier semantics, 2026-08-26)."""
  cursor.execute("""
      SELECT COALESCE(SUM(load_inches), 0) AS planned
      FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND line_status IN %s AND tier <> 'Stretch'
  """, (plan_date, _COUNTS_AGAINST_CAP))
  return float(cursor.fetchone()['planned'])


def _suggest_day(cursor, load_inches, after_date, max_days=10):
  """Smallest future business day whose planned total + this line fits
  its capacity estimate. None if nothing fits inside the window."""
  d = after_date
  for _ in range(max_days):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:                      # skip Sat/Sun
      d = d + timedelta(days=1)
    cap = _capacity_for(cursor, d)['capacity_inches_effective']
    if _planned_inches(cursor, d) + load_inches <= cap:
      return d.isoformat()
  return None


@anvil.server.callable
def ship_plan_get_capacity(plan_date):
  conn = get_connection()
  try:
    cursor = conn.cursor()
    return {'status': 'success',
            'capacity': _capacity_for(cursor, _parse_date(plan_date))}
  except Exception as e:
    return {'status': 'error', 'message': f'Capacity error: {e}'}
  finally:
    conn.close()


# =============================================================================
# CANDIDATES
# =============================================================================

@anvil.server.callable
def ship_plan_get_candidates(search=None, bucket=None, due_within_days=None,
                             special_only=False, defaulted_only=False,
                             limit=1000, flag_filter=None):
  """Left pane. The already-planned guard lives in the VIEW.
  due_within_days='auto' means: read PLAN_HORIZON_DAYS here, server-side
  — the client computes NOTHING, so the parameter takes effect the
  moment it is saved (2026-08-27 bug: the client cached 10 forever).
  2026-08-31 flag system (040/041): joined to vw_pc_order_flag_scores —
  each row carries is_gated / gate_codes / priority_score / flag_codes
  plus 'flags' = [{code, name, note}] for tooltips; sort = gated last,
  then priority_score DESC, then oldest ship-by.
  flag_filter (2026-09-01, the single Flag dropdown): None/'ALL' = all;
  'PLANNABLE' / 'GATED' = by is_gated; 'ANY_A' / 'ANY_B' = carries any
  flag of that category; or one code ('X03', 'B02') = carries it.
  Applied server-side so the header count stays honest."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    horizon_days = None
    if due_within_days == 'auto':
      cursor.execute("""
          SELECT param_value FROM dbo.pc_plan_parameters
          WHERE param_code = 'PLAN_HORIZON_DAYS'
      """)
      prow = cursor.fetchone()
      try:
        due_within_days = int(float(prow['param_value'])) if prow else 10
      except (TypeError, ValueError):
        due_within_days = 10
    if due_within_days:
      horizon_days = int(due_within_days)
    where, args = [], []
    if search:
      like = f'%{search.strip()}%'
      where.append("(c.nx_order_numb LIKE %s OR c.nx_sku LIKE %s "
                   "OR c.sku_desc LIKE %s OR c.shipto_code LIKE %s)")
      args += [like, like, like, like]
    if bucket in ('ST', 'ZERO', 'OTHER'):
      where.append("c.bucket = %s")
      args.append(bucket)
    if due_within_days:
      # BUSINESS days, as the label promises (Dave 2026-08-27: 4 days
      # reached Monday — calendar counting was crossing the weekend).
      end, added = date.today(), 0
      while added < int(due_within_days):
        end += timedelta(days=1)
        if end.weekday() < 5:
          added += 1
      where.append("c.ship_by_date <= %s")
      args.append(end)
    if special_only:
      where.append("c.n_non_standard > 0")
    if defaulted_only:
      where.append("c.n_class_defaulted > 0")
    ff = (flag_filter or 'ALL').strip().upper()
    if ff == 'PLANNABLE':
      where.append("ISNULL(fs.is_gated, 0) = 0")
    elif ff == 'GATED':
      where.append("ISNULL(fs.is_gated, 0) = 1")
    elif ff in ('ANY_A', 'ANY_B'):
      # flag_codes is a CSV ('A01,A05,B02'); wrap in commas, LIKE ',A__,'
      where.append("',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s")
      args.append('%,' + ff[-1] + '__,%')
    elif (len(ff) == 3 and ff[0] in 'XABC' and ff[1:].isdigit()):
      where.append("',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s")
      args.append('%,' + ff + ',%')
    # One FROM for all three queries below: the scores join is needed by
    # the filter, the count and the sort alike.
    from_sql = ("FROM dbo.vw_pc_ship_candidates c "
                "LEFT JOIN dbo.vw_pc_order_flag_scores fs "
                "ON fs.nx_order_numb = c.nx_order_numb "
                "AND fs.nx_sku = c.nx_sku")
    # Flag system (040/041): the scores view drives eligibility + rank.
    # Gated rows sort LAST (they are the Daily conversation, not the
    # plan); ungated rank by priority_score, then oldest ship-by.
    sql = ("SELECT TOP %d c.*, ISNULL(fs.is_gated, 0) AS is_gated, "
           "fs.gate_codes, "
           "ISNULL(fs.priority_score, 0) AS priority_score, "
           "fs.flag_codes " % max(1, min(int(limit), 1000))) + from_sql
    if where:
      sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY is_gated, priority_score DESC, "
            "c.ship_by_date, c.load_inches DESC")
    cursor.execute(sql, tuple(args))
    rows = [serialize_row(r) for r in cursor.fetchall()]
    # Total matching (pre-TOP) — so truncation is VISIBLE, never silent:
    # with a deep overdue backlog the oldest rows can fill the cap and
    # hide the future entirely (the "list ends at 8/24" mystery).
    count_sql = "SELECT COUNT(*) AS n " + from_sql
    if where:
      count_sql += " WHERE " + " AND ".join(where)
    cursor.execute(count_sql, tuple(args))
    total = (cursor.fetchone() or {}).get('n', len(rows))
    # Per-flag detail (code + note) for row tooltips — same WHERE, so
    # the notes describe exactly the rows on screen. One more query,
    # still ONE round trip for the client.
    flags_sql = ("SELECT f.nx_order_numb, f.nx_sku, f.flag_code, "
                 "f.flag_name, f.note "
                 "FROM dbo.vw_pc_order_flags f "
                 "JOIN dbo.vw_pc_ship_candidates c "
                 "ON c.nx_order_numb = f.nx_order_numb "
                 "AND c.nx_sku = f.nx_sku "
                 "LEFT JOIN dbo.vw_pc_order_flag_scores fs "
                 "ON fs.nx_order_numb = c.nx_order_numb "
                 "AND fs.nx_sku = c.nx_sku")
    if where:
      flags_sql += " WHERE " + " AND ".join(where)
    cursor.execute(flags_sql, tuple(args))
    fmap = {}
    for fr in cursor.fetchall():
      fmap.setdefault((fr['nx_order_numb'], fr['nx_sku']), []).append(
        {'code': fr['flag_code'], 'name': fr['flag_name'],
         'note': fr['note']})
    for r in rows:
      r['flags'] = fmap.get((r['nx_order_numb'], r['nx_sku']), [])
    # Feed freshness for the always-on Orders chip — age computed
    # SERVER-side (GETDATE) so browser clocks can't skew it.
    cursor.execute("""
        SELECT MAX(nx_last_updated) AS feed_last_updated,
               DATEDIFF(minute, MAX(nx_last_updated), GETDATE()) AS feed_age_minutes
        FROM dbo.sales_orders_due
    """)
    f = cursor.fetchone() or {}
    return {'status': 'success', 'candidates': rows,
            'total_count': total, 'horizon_days': horizon_days,
            'feed_last_updated': make_serializable(f.get('feed_last_updated')),
            'feed_age_minutes': f.get('feed_age_minutes')}
  except Exception as e:
    return {'status': 'error', 'message': f'Candidate load error: {e}'}
  finally:
    conn.close()


# =============================================================================
# PLAN READ (+ drift REPORT — never a re-total)
# =============================================================================

@anvil.server.callable
def ship_plan_get_plan(plan_date):
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM dbo.pc_ship_plan WHERE plan_date = %s",
                   (plan_date,))
    header = cursor.fetchone()

    cursor.execute("""
        SELECT l.*, live.live_qty, pr.description AS reason_desc
        FROM dbo.pc_ship_plan_line l
        OUTER APPLY (SELECT SUM(s.nx_qty_balance_due) AS live_qty
                     FROM dbo.sales_orders_due s
                     WHERE s.nx_order_numb = l.nx_order_numb
                       AND s.nx_sku = l.nx_sku) live
        LEFT JOIN dbo.pc_priority_reasons pr
               ON pr.reason_code = l.priority_reason
        WHERE l.plan_date = %s
        ORDER BY CASE l.line_status WHEN 'Unplaced' THEN 0 ELSE 1 END,
                 CASE l.tier WHEN 'Must' THEN 0 WHEN 'Next' THEN 1
                             ELSE 2 END,
                 l.position, l.load_inches DESC
    """, (plan_date,))
    lines = []
    for r in cursor.fetchall():
      row = serialize_row(r)
      live = row.get('live_qty')
      planned = float(row['qty_planned'])
      if row['line_status'] in _ACTIVE_STATUSES:
        if live is None:
          row['drift'] = 'gone'          # nothing open — likely shipped
        elif abs(float(live) - planned) > 0.005:
          row['drift'] = f"{planned:g} → {float(live):g}"
        else:
          row['drift'] = None
      else:
        row['drift'] = None
      lines.append(row)

    planned_total = sum(float(l['load_inches']) for l in lines
                        if l['line_status'] in _COUNTS_AGAINST_CAP
                        and l.get('tier') != 'Stretch')
    stretch_total = sum(float(l['load_inches']) for l in lines
                        if l['line_status'] in _COUNTS_AGAINST_CAP
                        and l.get('tier') == 'Stretch')
    return {'status': 'success',
            'header': serialize_row(header) if header else None,
            'lines': lines,
            'planned_inches': round(planned_total, 1),
            'stretch_inches': round(stretch_total, 1),
            'capacity': _capacity_for(cursor, plan_date)}
  except Exception as e:
    return {'status': 'error', 'message': f'Plan load error: {e}'}
  finally:
    conn.close()


# =============================================================================
# MUTATIONS — one transaction each, event row inside it
# =============================================================================

def _fetch_candidate_rows(cursor, order, sku):
  cursor.execute("""
      SELECT * FROM dbo.vw_pc_ship_candidate_rows
      WHERE nx_order_numb = %s AND nx_sku = %s
  """, (order, sku))
  return cursor.fetchall()


def _aggregate(rows):
  qty = sum(float(r['nx_qty_balance_due'] or 0) for r in rows)
  dates = [r['nx_dship_by_date'] or r['nx_hship_by_date'] for r in rows]
  dates = [d.date() if isinstance(d, datetime) else d for d in dates if d]
  return {
    'qty': qty,
    'ship_by': min(dates) if dates else None,
    'shipto': rows[0]['nx_shipto_code'],
    'carrier': rows[0]['nx_carrier'],
    'minutes': sum(float(r['minutes_charged'] or 0) for r in rows),
    'surcharge': sum(float(r['surcharge_minutes'] or 0) for r in rows),
    'inches': sum(float(r['work_inches'] or 0) for r in rows),
  }


def _insert_line_and_source(cursor, plan_date, order, sku, rows, agg,
                            role='PC', actor=None, tier='Next', reason=None):
  cursor.execute("""
      INSERT INTO dbo.pc_ship_plan_line
          (plan_date, nx_order_numb, nx_sku, qty_planned, ship_by_date,
           shipto_code, carrier, load_inches, load_minutes,
           surcharge_minutes, line_status, added_by_role, created_by,
           tier, priority_reason)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Planned', %s, %s,
              %s, %s)
  """, (plan_date, order, sku, agg['qty'], agg['ship_by'], agg['shipto'],
        agg['carrier'], agg['inches'], agg['minutes'], agg['surcharge'],
        role, actor or _current_actor(), tier, (reason or None)))
  for r in rows:
    cursor.execute("""
        INSERT INTO dbo.pc_ship_plan_line_source
            (plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no,
             qty_balance, nx_attrib_val, element_class_used,
             class_was_defaulted, size_inches, minutes_charged)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (plan_date, order, sku, r['nx_rel_numb'], r['nx_item_no'],
          r['nx_qty_balance_due'], r['nx_attrib_val'],
          (r['element_class'] if r['element_class'] in
           ('Standard', 'Non-Standard') else 'n/a'),
          r['class_was_defaulted'], r['size_inches'], r['minutes_charged']))


def _ensure_header(cursor, plan_date, actor):
  cursor.execute("SELECT plan_date FROM dbo.pc_ship_plan WHERE plan_date = %s",
                 (plan_date,))
  if not cursor.fetchone():
    weights = _get_active_weights(cursor)
    cursor.execute("""
        INSERT INTO dbo.pc_ship_plan (plan_date, weight_set_id, created_by)
        VALUES (%s, %s, %s)
    """, (plan_date, weights['weight_set_id'], actor))


@anvil.server.callable
def ship_plan_add_line(plan_date, order, sku, tier='Next', reason=None):
  """Add an (order, SKU) to a day. The line is ALWAYS added — over-cap is
  reported back so the client runs the cap-hit dialog (snooze / unplaced /
  stretch / keep). Never a silent drop, never a lost order."""
  plan_date = _parse_date(plan_date)
  if tier not in ('Must', 'Next', 'Stretch'):
    return {'status': 'error', 'message': f'Bad tier {tier}.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT line_status FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (plan_date, order, sku))
    existing = cursor.fetchone()
    if existing and existing['line_status'] != 'Removed':
      return {'status': 'error',
              'message': f'{order} / {sku} is already on this plan '
              f"({existing['line_status']})."}

    rows = _fetch_candidate_rows(cursor, order, sku)
    if not rows:
      return {'status': 'error',
              'message': f'{order} / {sku} has no open balance in the feed '
              '(shipped, or already planned on another day).'}
    agg = _aggregate(rows)
    _ensure_header(cursor, plan_date, actor)

    if existing:  # was Removed — revive rather than violate the PK
      cursor.execute("""
          DELETE FROM dbo.pc_ship_plan_line_source
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
      """, (plan_date, order, sku))
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET qty_planned = %s, ship_by_date = %s, shipto_code = %s,
              carrier = %s, load_inches = %s, load_minutes = %s,
              surcharge_minutes = %s, line_status = 'Planned',
              tier = %s, priority_reason = %s,
              snooze_to_date = NULL, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
      """, (agg['qty'], agg['ship_by'], agg['shipto'], agg['carrier'],
            agg['inches'], agg['minutes'], agg['surcharge'],
            tier, (reason or None), actor,
            plan_date, order, sku))
      for r in rows:
        cursor.execute("""
            INSERT INTO dbo.pc_ship_plan_line_source
                (plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no,
                 qty_balance, nx_attrib_val, element_class_used,
                 class_was_defaulted, size_inches, minutes_charged)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (plan_date, order, sku, r['nx_rel_numb'], r['nx_item_no'],
              r['nx_qty_balance_due'], r['nx_attrib_val'],
              (r['element_class'] if r['element_class'] in
               ('Standard', 'Non-Standard') else 'n/a'),
              r['class_was_defaulted'], r['size_inches'],
              r['minutes_charged']))
    else:
      _insert_line_and_source(cursor, plan_date, order, sku, rows, agg,
                              actor=actor, tier=tier, reason=reason)

    _log_event(cursor, plan_date, order, sku, 'Added',
               new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in "
               f"({tier})",
               actor=actor)
    _bump_revision(cursor, plan_date, actor)

    planned = _planned_inches(cursor, plan_date)
    cap = _capacity_for(cursor, plan_date)['capacity_inches_effective']
    over = planned > cap
    suggested = _suggest_day(cursor, agg['inches'], plan_date) if over else None
    conn.commit()
    return {'status': 'success', 'planned_inches': round(planned, 1),
            'capacity_inches': round(cap, 1), 'over_capacity': over,
            'line_inches': round(agg['inches'], 1),
            'suggested_day': suggested}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Add failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_get_reasons():
  """Active priority reasons, dropdown-ready. Managed in
  pc_priority_reasons (add rows there; no code change)."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT reason_code, description
        FROM dbo.pc_priority_reasons
        WHERE is_active = 1
        ORDER BY sort_order, reason_code
    """)
    return {'status': 'success',
            'reasons': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_set_priority(plan_date, order, sku, tier=None, reason=None,
                           note=None):
  """Set tier / reason / note on one plan line. Pass only what changes;
  ''=clear. Each change gets its own event (TierChange / ReasonSet) and
  counts as a revision on a Published plan. The note is the text behind
  reason OTHER ('Other (see note)') — optional color on any reason."""
  plan_date = _parse_date(plan_date)
  if tier is not None and tier not in ('Must', 'Next', 'Stretch'):
    return {'status': 'error', 'message': f'Bad tier {tier}.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT tier, priority_reason, priority_note, line_status
        FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (plan_date, order, sku))
    row = cursor.fetchone()
    if not row:
      return {'status': 'error', 'message': 'Line not found.'}
    if row['line_status'] == 'Removed':
      return {'status': 'error', 'message': 'Line is removed — revive it '
              'first (re-add the order).'}

    changed = False
    if tier is not None and tier != row['tier']:
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET tier = %s, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
      """, (tier, actor, plan_date, order, sku))
      _log_event(cursor, plan_date, order, sku, 'TierChange',
                 old_value=row['tier'], new_value=tier, actor=actor)
      changed = True
    if reason is not None:
      new_reason = reason or None          # '' clears
      if new_reason != row['priority_reason']:
        cursor.execute("""
            UPDATE dbo.pc_ship_plan_line
            SET priority_reason = %s, updated_by = %s, updated_at = GETDATE()
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
        """, (new_reason, actor, plan_date, order, sku))
        _log_event(cursor, plan_date, order, sku, 'ReasonSet',
                   old_value=row['priority_reason'], new_value=new_reason,
                   actor=actor)
        changed = True
    if note is not None:
      new_note = (note or '').strip()[:200] or None
      if new_note != row['priority_note']:
        cursor.execute("""
            UPDATE dbo.pc_ship_plan_line
            SET priority_note = %s, updated_by = %s, updated_at = GETDATE()
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
        """, (new_note, actor, plan_date, order, sku))
        _log_event(cursor, plan_date, order, sku, 'ReasonSet',
                   old_value=row['priority_note'], new_value=new_note,
                   actor=actor, note='note edit')
        changed = True

    if not changed:
      conn.rollback()
      return {'status': 'success', 'changed': False}
    _bump_revision(cursor, plan_date, actor)
    planned = _planned_inches(cursor, plan_date)
    cap = _capacity_for(cursor, plan_date)['capacity_inches_effective']
    conn.commit()
    return {'status': 'success', 'changed': True,
            'planned_inches': round(planned, 1),
            'capacity_inches': round(cap, 1),
            'over_capacity': planned > cap}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Priority failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_add_order(plan_date, order, tier='Next', reason=None):
  """Add EVERY open SKU of a sales order to the day — one transaction.
  Per-line Added events plus one OrderAdded summary; a single cap check
  at the end (the client runs one dialog for the whole order, not one
  per line). SKUs already on the plan are skipped, not errors."""
  plan_date = _parse_date(plan_date)
  if tier not in ('Must', 'Next', 'Stretch'):
    return {'status': 'error', 'message': f'Bad tier {tier}.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT nx_sku FROM dbo.vw_pc_ship_candidate_rows
        WHERE nx_order_numb = %s
    """, (order,))
    skus = [r['nx_sku'] for r in cursor.fetchall()]
    if not skus:
      return {'status': 'error',
              'message': f'{order} has no open balance in the feed '
              '(shipped, or already planned on another day).'}

    _ensure_header(cursor, plan_date, actor)
    added, skipped, order_inches = [], [], 0.0
    for sku in skus:
      cursor.execute("""
          SELECT line_status FROM dbo.pc_ship_plan_line
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
      """, (plan_date, order, sku))
      existing = cursor.fetchone()
      if existing and existing['line_status'] != 'Removed':
        skipped.append(sku)
        continue
      rows = _fetch_candidate_rows(cursor, order, sku)
      if not rows:
        skipped.append(sku)
        continue
      agg = _aggregate(rows)
      if existing:  # Removed — revive: clear then reinsert cleanly
        cursor.execute("""
            DELETE FROM dbo.pc_ship_plan_line_source
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
        """, (plan_date, order, sku))
        cursor.execute("""
            DELETE FROM dbo.pc_ship_plan_line
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
        """, (plan_date, order, sku))
      _insert_line_and_source(cursor, plan_date, order, sku, rows, agg,
                              actor=actor, tier=tier, reason=reason)
      _log_event(cursor, plan_date, order, sku, 'Added',
                 new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in "
                 f"({tier})",
                 actor=actor, note='whole order')
      added.append(sku)
      order_inches += agg['inches']

    if not added:
      conn.rollback()
      return {'status': 'error',
              'message': f'Every open item of {order} is already on this '
              'plan — nothing to add.'}

    _log_event(cursor, plan_date, order, None, 'OrderAdded',
               new_value=f'{len(added)} items / {order_inches:.1f} in '
               f'({tier})',
               actor=actor)
    _bump_revision(cursor, plan_date, actor)

    planned = _planned_inches(cursor, plan_date)
    cap = _capacity_for(cursor, plan_date)['capacity_inches_effective']
    over = planned > cap
    suggested = (_suggest_day(cursor, order_inches, plan_date)
                 if over else None)
    conn.commit()
    return {'status': 'success', 'added': added, 'skipped': skipped,
            'planned_inches': round(planned, 1),
            'capacity_inches': round(cap, 1), 'over_capacity': over,
            'line_inches': round(order_inches, 1),
            'suggested_day': suggested}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Add order failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_remove_order(plan_date, order, note=None):
  """Remove every active line of a sales order from the day — one
  transaction, per-line Removed events plus one OrderRemoved summary.
  Removed is a status, never a delete."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s
          AND line_status <> 'Removed'
    """, (plan_date, order))
    skus = [r['nx_sku'] for r in cursor.fetchall()]
    if not skus:
      return {'status': 'error',
              'message': f'{order} has no active lines on this plan.'}
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Removed', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s
          AND line_status <> 'Removed'
    """, (actor, plan_date, order))
    for sku in skus:
      _log_event(cursor, plan_date, order, sku, 'Removed',
                 actor=actor, note=note or 'whole order')
    _log_event(cursor, plan_date, order, None, 'OrderRemoved',
               new_value=f'{len(skus)} items', actor=actor, note=note)
    _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success', 'removed': skus}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Remove order failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_remove_line(plan_date, order, sku, note=None):
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Removed', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
          AND line_status <> 'Removed'
    """, (_current_actor(), plan_date, order, sku))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'Line not found (or already removed).'}
    _log_event(cursor, plan_date, order, sku, 'Removed', note=note)
    _bump_revision(cursor, plan_date, None)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Remove failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_mark_unplaced(plan_date, order, sku, note=None):
  """The explicit queue (DECISION §4) — visible on the day PC tried."""
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Unplaced', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (_current_actor(), plan_date, order, sku))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'Line not found.'}
    _log_event(cursor, plan_date, order, sku, 'Unplaced', note=note)
    _bump_revision(cursor, plan_date, None)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Unplace failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_snooze_line(plan_date, order, sku, to_date):
  """Mark this day's line Snoozed and create the future day's line —
  ONE transaction across both days. Source rows re-captured fresh for the
  new day (its own plan-time snapshot)."""
  plan_date = _parse_date(plan_date)
  to_date = _parse_date(to_date)
  if to_date <= plan_date:
    return {'status': 'error', 'message': 'Snooze target must be a later day.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT line_status FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (to_date, order, sku))
    if cursor.fetchone():
      return {'status': 'error',
              'message': f'{order} / {sku} is already on {to_date}.'}
    cursor.execute("""
        SELECT tier, priority_reason FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (plan_date, order, sku))
    src = cursor.fetchone() or {}

    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Snoozed', snooze_to_date = %s,
            updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
          AND line_status IN %s
    """, (to_date, actor, plan_date, order, sku, _ACTIVE_STATUSES))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'Line not found or not active.'}
    _log_event(cursor, plan_date, order, sku, 'Snoozed',
               new_value=to_date.isoformat(), actor=actor)

    rows = _fetch_candidate_rows(cursor, order, sku)
    if not rows:
      conn.rollback()
      return {'status': 'error',
              'message': 'No open balance left in the feed — nothing to '
              'snooze forward. Refresh the plan.'}
    agg = _aggregate(rows)
    _ensure_header(cursor, to_date, actor)
    _insert_line_and_source(cursor, to_date, order, sku, rows, agg,
                            actor=actor, tier=src.get('tier') or 'Next',
                            reason=src.get('priority_reason'))
    _log_event(cursor, to_date, order, sku, 'Added',
               new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in",
               actor=actor, note=f'snoozed from {plan_date}')
    _bump_revision(cursor, plan_date, actor)
    _bump_revision(cursor, to_date, actor)
    conn.commit()
    return {'status': 'success', 'to_date': to_date.isoformat()}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Snooze failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_recalc_line(plan_date, order, sku):
  """THE drift-rule writer — the only path that re-totals a plan line.
  Replaces the source snapshot and logs Recalc with old/new."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT qty_planned, load_inches FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
          AND line_status IN %s
    """, (plan_date, order, sku, _ACTIVE_STATUSES))
    old = cursor.fetchone()
    if not old:
      return {'status': 'error', 'message': 'Line not found or not active.'}

    rows = _fetch_candidate_rows(cursor, order, sku)
    if not rows:
      return {'status': 'error',
              'message': 'No open balance in the feed — this line likely '
              'shipped. Remove it rather than refreshing.'}
    agg = _aggregate(rows)

    cursor.execute("""
        DELETE FROM dbo.pc_ship_plan_line_source
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (plan_date, order, sku))
    for r in rows:
      cursor.execute("""
          INSERT INTO dbo.pc_ship_plan_line_source
              (plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no,
               qty_balance, nx_attrib_val, element_class_used,
               class_was_defaulted, size_inches, minutes_charged)
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
      """, (plan_date, order, sku, r['nx_rel_numb'], r['nx_item_no'],
            r['nx_qty_balance_due'], r['nx_attrib_val'],
            (r['element_class'] if r['element_class'] in
             ('Standard', 'Non-Standard') else 'n/a'),
            r['class_was_defaulted'], r['size_inches'], r['minutes_charged']))
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET qty_planned = %s, ship_by_date = %s, load_inches = %s,
            load_minutes = %s, surcharge_minutes = %s,
            updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
    """, (agg['qty'], agg['ship_by'], agg['inches'], agg['minutes'],
          agg['surcharge'], actor, plan_date, order, sku))

    old_txt = f"{float(old['qty_planned']):g} pcs / {float(old['load_inches']):.1f} in"
    new_txt = f"{agg['qty']:g} pcs / {agg['inches']:.1f} in"
    _log_event(cursor, plan_date, order, sku, 'Recalc',
               old_value=old_txt, new_value=new_txt, actor=actor)
    _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success', 'old': old_txt, 'new': new_txt}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Refresh failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_publish(plan_date):
  """Stamp capacity (the number sales saw) and flip Draft -> Published.
  The plan stays editable after — real life happens; edits keep logging."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cap = _capacity_for(cursor, plan_date)
    cursor.execute("""
        UPDATE dbo.pc_ship_plan
        SET plan_status = 'Published', capacity_inches = %s,
            capacity_detail = %s, published_by = %s, published_at = GETDATE()
        WHERE plan_date = %s
    """, (cap['capacity_inches_effective'], cap['capacity_detail'][:200],
          actor, plan_date))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'No plan exists for that day yet.'}
    _log_event(cursor, plan_date, None, None, 'Published',
               new_value=f"capacity {cap['capacity_inches_effective']:.0f} in",
               actor=actor, note=cap['capacity_detail'][:500])
    conn.commit()
    return {'status': 'success', 'capacity': cap}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Publish failed: {e}'}
  finally:
    conn.close()


# =============================================================================
# PARAMETERS / OVERRIDES / SPECIAL CUSTOMERS / EVENTS
# =============================================================================

@anvil.server.callable
def ship_plan_get_day(plan_date, search=None, bucket=None,
                      due_within_days=None, special_only=False,
                      defaulted_only=False, flag_filter=None):
  """Plan + candidates in ONE round trip. Anvil charges ~1s of transport
  per call regardless of query size, so the form's load path (and every
  dialog-close reload) pays it once, not twice."""
  plan = ship_plan_get_plan(plan_date)
  if plan.get('status') != 'success':
    return plan
  cands = ship_plan_get_candidates(search=search, bucket=bucket,
                                   due_within_days=due_within_days,
                                   special_only=special_only,
                                   defaulted_only=defaulted_only,
                                   flag_filter=flag_filter)
  if cands.get('status') != 'success':
    return cands
  # Flag definitions ride along (22 tiny rows): the legend popup, the
  # Flag dropdown items and the How-To all read the SAME live table, so
  # a weight or threshold change shows up on the next load — no extra
  # round trip, no hardcoded legend to go stale.
  defs = ship_plan_get_flag_definitions()
  return {'status': 'success', 'plan': plan, 'candidates': cands,
          'flag_definitions': defs.get('definitions', [])}


@anvil.server.callable
def ship_plan_get_flag_definitions():
  """Live legend source: dbo.pc_flag_definitions (migration 040), all
  rows incl. inactive Phase-2 ones (shown greyed), category order
  X, A, B, C then code."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT flag_code, category, flag_name, description, weight,
               threshold_value, is_manual, display_glyph, phase, active
        FROM dbo.pc_flag_definitions
        ORDER BY CASE category WHEN 'X' THEN 0 WHEN 'A' THEN 1
                               WHEN 'B' THEN 2 ELSE 3 END, flag_code
    """)
    return {'status': 'success',
            'definitions': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'Flag definitions error: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_get_params_bundle(override_date=None):
  """Everything the Parameters panel shows, in ONE round trip (it used
  to make three sequential calls — three seconds of frozen spinner)."""
  p = ship_plan_get_parameters()
  if p.get('status') != 'success':
    return p
  c = ship_plan_get_special_customers()
  if c.get('status') != 'success':
    return c
  # overrides only when a date is asked for — the Parameters popup no
  # longer shows that section (it lives in Mark absent / trainee...)
  o = (ship_plan_get_overrides(override_date) if override_date
       else {'status': 'success', 'overrides': []})
  if o.get('status') != 'success':
    return o
  return {'status': 'success', 'parameters': p['parameters'],
          'customers': c['customers'], 'overrides': o['overrides']}


@anvil.server.callable
def ship_plan_get_parameters():
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT param_code, param_value, description, updated_by, updated_at
        FROM dbo.pc_plan_parameters ORDER BY param_code
    """)
    return {'status': 'success',
            'parameters': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_set_parameter(param_code, param_value):
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT param_value FROM dbo.pc_plan_parameters WHERE param_code = %s
    """, (param_code,))
    old = cursor.fetchone()
    if not old:
      return {'status': 'error', 'message': f'Unknown parameter {param_code}.'}
    cursor.execute("""
        UPDATE dbo.pc_plan_parameters
        SET param_value = %s, updated_by = %s, updated_at = GETDATE()
        WHERE param_code = %s
    """, (str(param_value).strip(), actor, param_code))
    _log_event(cursor, date.today(), None, None, 'ParamChange',
               old_value=f"{param_code}={old['param_value']}",
               new_value=f"{param_code}={str(param_value).strip()}",
               actor=actor)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_get_overrides(for_date):
  """Active roster x that date's snapshot + overrides — the panel shows
  what PC would be out-voting."""
  for_date = _parse_date(for_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.employee_id,
               COALESCE(s.full_name, 'emp ' + r.employee_id) AS full_name,
               r.shift, s.status AS snapshot_status,
               r.default_capacity_factor,
               o.override_type, o.capacity_factor, o.note
        FROM dbo.em_assembler_roster r
        LEFT JOIN dbo.em_assembler_status_daily s
               ON s.employee_id = r.employee_id AND s.status_date = %s
        LEFT JOIN dbo.pc_assembler_day_override o
               ON o.employee_id = r.employee_id AND o.override_date = %s
        WHERE r.end_date IS NULL
        ORDER BY r.shift, full_name
    """, (for_date, for_date))
    return {'status': 'success',
            'overrides': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_set_override(for_date, employee_id, override_type,
                           capacity_factor=None, note=None):
  """override_type None clears the override (snapshot rules again)."""
  for_date = _parse_date(for_date)
  actor = _current_actor()
  factors = {'Absent': 0.0, 'Present': 1.0, 'HalfDay': 0.5}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    if override_type is None:
      cursor.execute("""
          DELETE FROM dbo.pc_assembler_day_override
          WHERE override_date = %s AND employee_id = %s
      """, (for_date, employee_id))
      _log_event(cursor, for_date, None, None, 'ParamChange',
                 old_value=f'override {employee_id}',
                 new_value='cleared', actor=actor)
      conn.commit()
      return {'status': 'success'}

    if override_type not in ('Absent', 'Present', 'Trainee', 'HalfDay'):
      return {'status': 'error', 'message': f'Bad type {override_type}.'}
    if capacity_factor is None:
      if override_type == 'Trainee':
        params = _get_params(cursor)
        capacity_factor = float(params.get('TRAINEE_MULTIPLIER', 0.5))
      else:
        capacity_factor = factors[override_type]

    cursor.execute("""
        UPDATE dbo.pc_assembler_day_override
        SET override_type = %s, capacity_factor = %s, note = %s,
            entered_by = %s, entered_at = GETDATE()
        WHERE override_date = %s AND employee_id = %s
    """, (override_type, capacity_factor, note, actor, for_date, employee_id))
    if cursor.rowcount == 0:
      cursor.execute("""
          INSERT INTO dbo.pc_assembler_day_override
              (override_date, employee_id, override_type, capacity_factor,
               note, entered_by)
          VALUES (%s, %s, %s, %s, %s, %s)
      """, (for_date, employee_id, override_type, capacity_factor, note, actor))
    _log_event(cursor, for_date, None, None, 'ParamChange',
               new_value=f'{employee_id} {override_type} x{capacity_factor}',
               actor=actor, note=note)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_get_special_customers():
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT customer_code, customer_name, surcharge_basis,
               surcharge_value, is_active, notes
        FROM dbo.pc_special_customers ORDER BY customer_code
    """)
    return {'status': 'success',
            'customers': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_upsert_special_customer(customer_code, customer_name,
                                      surcharge_value, is_active=True,
                                      notes=None):
  actor = _current_actor()
  code = (customer_code or '').strip().upper()[:15]
  if not code:
    return {'status': 'error', 'message': 'Customer code (prefix) required.'}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dbo.pc_special_customers
        SET customer_name = %s, surcharge_value = %s, is_active = %s,
            notes = %s
        WHERE customer_code = %s
    """, (customer_name, surcharge_value, (1 if is_active else 0), notes, code))
    verb = 'updated'
    if cursor.rowcount == 0:
      cursor.execute("""
          INSERT INTO dbo.pc_special_customers
              (customer_code, customer_name, surcharge_value, is_active,
               notes, added_by)
          VALUES (%s, %s, %s, %s, %s, %s)
      """, (code, customer_name, surcharge_value, (1 if is_active else 0),
            notes, actor))
      verb = 'added'
    _log_event(cursor, date.today(), None, None, 'ParamChange',
               new_value=f'special customer {code} {verb} '
               f'x{surcharge_value} active={bool(is_active)}',
               actor=actor)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_get_events(plan_date, limit=100):
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP %d event_id, plan_date, nx_order_numb, nx_sku, event_type,
               old_value, new_value, actor, event_time, note
        FROM dbo.pc_plan_line_events
        WHERE plan_date = %%s
        ORDER BY event_time DESC, event_id DESC
    """ % max(1, min(int(limit), 500)), (plan_date,))
    return {'status': 'success',
            'events': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()