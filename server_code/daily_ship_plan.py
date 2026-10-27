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

066 (2026-09-15, Dave): the plan line IS the e21 line - identity =
    (plan_date, nx_order_numb, nx_rel_numb, nx_item_no); nx_sku is an
    attribute. Line callables take (plan_date, order, rel, item, ...).
    Planning quantity = nx_qty_ordered (e21 orditem.qty); balance due is
    display + the C05 warning only. One candidate row per e21 line.

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: August 2026
"""

import anvil.server
import threading
import os
import re
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
               old_value=None, new_value=None, actor=None, note=None,
               rel=None, item=None):
  """Append one pc_plan_line_events row. ALWAYS called inside the same
  transaction as the mutation it records. 066: rel/item identify the
  e21 line; order-level events leave them None."""
  cursor.execute("""
      INSERT INTO dbo.pc_plan_line_events
          (plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no, event_type,
           old_value, new_value, actor, note)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
  """, (plan_date, order, sku, rel, item, event_type,
        (str(old_value)[:200] if old_value is not None else None),
        (str(new_value)[:200] if new_value is not None else None),
        (actor or _current_actor()), (note[:500] if note else None)))


def _customer_label(name, city, state, fallback=None):
  """2026-09-15: 'FERGUSON ENTERPRISES #61 - EULESS, TX' for the Item
  column sub-line on both grids (sales asked for customer, not ship-to).
  Falls back to the ship-to code when e21 no longer has the line."""
  name = (name or '').strip()
  if not name:
    return fallback or ''
  place = ', '.join(p for p in ((city or '').strip(), (state or '').strip()) if p)
  return f"{name} - {place}" if place else name


def _line_label(order, rel, item, sku=None):
  """'595613-001-20' - order, RELEASE, item, always all three (Dave
  2026-09-15: a release shown only sometimes reads as an error), + ' / SKU'."""
  r, i = (rel or '').strip(), (item or '').strip()
  s = f"{order}-{r}-{i}" if (r or i) else f"{order}"
  return f"{s} / {sku}" if sku else s


def _stamp_work_orders(cursor, plan_date):
  """Migration 056 (2026-09-08): carry the published plan onto the work
  orders — plan_date / plan_tier / plan_position — so the Production App
  releases top-to-bottom and the Assembly App sees Must/Next/Stretch
  through vw_pc_department_queue. The logic lives in ONE place,
  dbo.sp_stamp_plan_work_orders (clears stale stamps for the day, then
  stamps every live work order behind a planned line). Silent no-op
  until 056 is applied. Returns {'cleared', 'stamped'} or None."""
  cursor.execute("SELECT OBJECT_ID('dbo.sp_stamp_plan_work_orders') AS o")
  if (cursor.fetchone() or {}).get('o') is None:
    return None
  cursor.execute("EXEC dbo.sp_stamp_plan_work_orders @plan_date = %s", (plan_date,))
  row = cursor.fetchone() or {}
  return {'cleared': int(row.get('cleared') or 0), 'stamped': int(row.get('stamped') or 0)}


def _bump_revision(cursor, plan_date, actor):
  """If the day's plan is already PUBLISHED, count this change as a
  revision — displayed as 'PUBLISHED 8:52 · rev 3'. Draft edits are not
  revisions; the agreement only exists once it's published.
  056: a revision re-stamps the day's work orders (added / removed lines
  reach the Production App at once)."""
  cursor.execute("""
      UPDATE dbo.pc_ship_plan
      SET revision_no = revision_no + 1,
          last_revised_at = GETDATE(), last_revised_by = %s
      WHERE plan_date = %s AND plan_status = 'Published'
  """, (actor or _current_actor(), plan_date))
  if cursor.rowcount:
    _stamp_work_orders(cursor, plan_date)


def _get_params(cursor):
  cursor.execute("SELECT param_code, param_value FROM dbo.pc_plan_parameters")
  return {r['param_code']: r['param_value'] for r in cursor.fetchall()}


def _stage_flags(cursor):
  """2026-09-28: evaluate vw_pc_order_flags ONCE per connection into the
  session temp tables #pcf (flags) and #pcs (the scores view's grouping,
  same columns as vw_pc_order_flag_scores). The candidate load used to
  evaluate the flag view three times (rows, count, notes) and the plan
  load once more; with 093's transfer-stage join that was ~3 s each on a
  quiet server and 12 s each on a busy one - the 08:24 timeout. Idempotent
  on the connection; temp tables vanish with it."""
  cursor.execute("""
      IF OBJECT_ID('tempdb..#pcf') IS NULL
      BEGIN
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, flag_code, category,
               flag_name, weight, note
        INTO #pcf FROM dbo.vw_pc_order_flags;
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku,
               CAST(MAX(CASE WHEN category = 'X' THEN 1 ELSE 0 END) AS BIT) AS is_gated,
               STRING_AGG(CASE WHEN category = 'X' THEN flag_code END, ',')
                   WITHIN GROUP (ORDER BY flag_code) AS gate_codes,
               SUM(CASE WHEN category <> 'X' THEN weight ELSE 0 END) AS priority_score,
               STRING_AGG(flag_code, ',') WITHIN GROUP (ORDER BY flag_code) AS flag_codes
        INTO #pcs FROM #pcf
        GROUP BY nx_order_numb, nx_rel_numb, nx_item_no, nx_sku;
        /* the costed feed too: the plan query's LEFT JOIN to
           vw_pc_ship_candidate_rows was re-evaluating the view per plan
           line (~100 ms x 40 lines = the 4 s plan load) */
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, nx_sku_desc, bucket,
               element_class, class_was_defaulted
        INTO #pcr FROM dbo.vw_pc_ship_candidate_rows;
      END
  """)


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

def _capacity_for(cursor, plan_date, prefer_estimate=False):
  """Capacity dict for a date. Today = vw_pc_capacity_day (the 08:45
  snapshot + overrides). Future = same formula with crew estimated as the
  full active roster, adjusted by that date's overrides — is_estimate=True
  so the bar renders hollow.
  prefer_estimate (agent, 06:30 — before the attendance snapshot loads):
  use the view only when its snapshot IS the plan date; otherwise the
  roster estimate with that date's overrides, never yesterday's crew."""
  today = date.today()
  if plan_date <= today:
    cursor.execute("SELECT * FROM dbo.vw_pc_capacity_day")
    row = cursor.fetchone()
    if row and prefer_estimate and row['snapshot_date'] != plan_date:
      row = None            # stale for the agent's purpose — fall through
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
  'GATED_DUE_IN' (075, 2026-09-18) = gated AND a PO is on the way for the
  SKU (gate X06) - the Sales / PC review list for "when can we ship it".
  Applied server-side so the header count stays honest."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    _stage_flags(cursor)                       # 2026-09-28: flag view once
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
      # 2026-09-25: the order-release-item label too ('608367-001-10'), so
      # the box behaves like the plan grid's (which matches line_label)
      where.append("(c.nx_order_numb LIKE %s OR c.nx_sku LIKE %s "
                   "OR c.sku_desc LIKE %s OR c.shipto_code LIKE %s "
                   "OR so.nx_cust_name LIKE %s "               # 2026-09-15: customer
                   "OR c.nx_order_numb + '-' + c.nx_rel_numb + '-' + c.nx_item_no LIKE %s)")
      args += [like, like, like, like, like, like]
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
    elif ff == 'GATED_DUE_IN':
      # 075: X06 rides in gate_codes (CSV) like any X code (parameter, not
      # a literal - the shared WHERE is %-formatted by pymssql)
      where.append("',' + ISNULL(fs.gate_codes, '') + ',' LIKE %s")
      args.append('%,X06,%')
    elif ff in ('ANY_A', 'ANY_B'):
      # flag_codes is a CSV ('A01,A05,B02'); wrap in commas, LIKE ',A__,'
      where.append("',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s")
      args.append('%,' + ff[-1] + '__,%')
    elif (len(ff) == 3 and ff[0] in 'XABC' and ff[1:].isdigit()):
      where.append("',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s")
      args.append('%,' + ff + ',%')
    # 091 rush lane: the same rule vw_pc_rush_candidates holds (expedited
    # A05 or PC's A04, ship-by on or before the next business day, not
    # gated), written inline so the candidate query stays one pass. The
    # glyph on the row and the 'RUSH' dropdown entry both come from here.
    rush_expr = ("(ISNULL(fs.is_gated, 0) = 0 "
                 "AND (',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s "
                 "OR ',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s) "
                 "AND c.ship_by_date <= %s)")
    rush_args = ['%,A05,%', '%,A04,%', _next_business_day(date.today() + timedelta(days=1))]
    if ff == 'RUSH':
      where.append(rush_expr)
      args += rush_args
    # One FROM for all three queries below: the scores join is needed by
    # the filter, the count and the sort alike.
    # 2026-09-15: the customer (name / city / state) rides along from the
    # feed row itself - one row per e21 line, so the join never fans out.
    cust_join = ("LEFT JOIN dbo.sales_orders_due so "
                 "ON so.nx_order_numb = c.nx_order_numb "
                 "AND so.nx_rel_numb = c.nx_rel_numb "
                 "AND so.nx_item_no = c.nx_item_no ")
    from_sql = ("FROM dbo.vw_pc_ship_candidates c "
                + cust_join +
                "LEFT JOIN #pcs fs "                       # staged scores
                "ON fs.nx_order_numb = c.nx_order_numb "
                "AND fs.nx_rel_numb = c.nx_rel_numb "
                "AND fs.nx_item_no = c.nx_item_no")
    # Flag system (040/041): the scores view drives eligibility + rank.
    # Gated rows sort LAST (they are the Daily conversation, not the
    # plan); ungated rank by priority_score, then oldest ship-by.
    # 066: one row per e21 line; qty_open = ordered, qty_balance = balance.
    sql = ("SELECT TOP %d c.*, ISNULL(fs.is_gated, 0) AS is_gated, "
           "fs.gate_codes, "
           "ISNULL(fs.priority_score, 0) AS priority_score, "
           "fs.flag_codes, "
           "so.nx_cust_name, so.nx_cust_city, so.nx_cust_state, "
           % max(1, min(int(limit), 1000))
           + "CASE WHEN " + rush_expr + " THEN 1 ELSE 0 END AS is_rush "   # 091
           + from_sql)
    if where:
      sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY is_gated, priority_score DESC, "
            "c.ship_by_date, c.load_inches DESC, c.nx_order_numb, "
            "c.nx_rel_numb, c.nx_item_no")
    cursor.execute(sql, tuple(rush_args + args))      # select-list params first
    rows = [serialize_row(r) for r in cursor.fetchall()]
    for r in rows:
      r['customer'] = _customer_label(r.get('nx_cust_name'), r.get('nx_cust_city'),
                                      r.get('nx_cust_state'), r.get('shipto_code'))
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
    flags_sql = ("SELECT f.nx_order_numb, f.nx_rel_numb, f.nx_item_no, "
                 "f.flag_code, f.flag_name, f.note "
                 "FROM #pcf f "                             # staged flags
                 "JOIN dbo.vw_pc_ship_candidates c "
                 "ON c.nx_order_numb = f.nx_order_numb "
                 "AND c.nx_rel_numb = f.nx_rel_numb "
                 "AND c.nx_item_no = f.nx_item_no "
                 + cust_join +                     # the search WHERE may name so.
                 "LEFT JOIN #pcs fs "
                 "ON fs.nx_order_numb = c.nx_order_numb "
                 "AND fs.nx_rel_numb = c.nx_rel_numb "
                 "AND fs.nx_item_no = c.nx_item_no")
    if where:
      flags_sql += " WHERE " + " AND ".join(where)
    cursor.execute(flags_sql, tuple(args))
    fmap = {}
    for fr in cursor.fetchall():
      fmap.setdefault((fr['nx_order_numb'], fr['nx_rel_numb'], fr['nx_item_no']), []).append(
        {'code': fr['flag_code'], 'name': fr['flag_name'],
         'note': fr['note']})
    for r in rows:
      r['flags'] = fmap.get((r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']), [])
    try:                                           # v1.6: multi-day standing on the candidate rows
      mdt = _md_candidate_text(cursor, rows)
      for r in rows:
        r['multiday'] = mdt.get((r['nx_order_numb'], r['nx_rel_numb'], str(r['nx_item_no'])))
    except Exception as e:
      for r in rows:
        r['multiday'] = None
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
    _stage_flags(cursor)                       # 2026-09-28: flag view once
    cursor.execute("SELECT * FROM dbo.pc_ship_plan WHERE plan_date = %s",
                   (plan_date,))
    header = cursor.fetchone()

    cursor.execute("""
        SELECT l.*, live.live_qty, live.live_ordered, pr.description AS reason_desc,
               live.nx_cust_name, live.nx_cust_city, live.nx_cust_state,
               cr.bucket, cr.nx_sku_desc AS sku_desc,
               CASE WHEN COALESCE(cr.element_class, src.element_class_used) = 'Non-Standard'
                    THEN 1 ELSE 0 END                                   AS n_non_standard,
               CAST(COALESCE(cr.class_was_defaulted, src.class_was_defaulted, 0) AS INT)
                                                                        AS n_class_defaulted,
               ISNULL(fs.is_gated, 0) AS is_gated, fs.gate_codes, fs.flag_codes,
               -- 091 rush lane: proposed by the rush poll, or meets the rule now
               CASE WHEN l.priority_reason = 'RUSH'
                      OR (ISNULL(fs.is_gated, 0) = 0
                          AND (',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s
                               OR ',' + ISNULL(fs.flag_codes, '') + ',' LIKE %s)
                          AND l.ship_by_date <= %s)
                    THEN 1 ELSE 0 END                                   AS is_rush
        FROM dbo.pc_ship_plan_line l
        OUTER APPLY (SELECT SUM(s.nx_qty_balance_due) AS live_qty,
                            SUM(ISNULL(s.nx_qty_ordered, s.nx_qty_balance_due)) AS live_ordered,
                            MAX(s.nx_cust_name) AS nx_cust_name,       -- 2026-09-15: customer
                            MAX(s.nx_cust_city) AS nx_cust_city,
                            MAX(s.nx_cust_state) AS nx_cust_state
                     FROM dbo.sales_orders_due s
                     WHERE s.nx_order_numb = l.nx_order_numb
                       AND s.nx_rel_numb = l.nx_rel_numb
                       AND s.nx_item_no = l.nx_item_no) live          -- 066: the e21 line
        -- 2026-09-15: the filter row applies to the plan grid too, so each
        -- line carries the same fields the candidate rows filter on. The
        -- live views (no already-planned guard) give bucket / desc / class /
        -- flags; the plan-time source row backs class up for a shipped line.
        LEFT JOIN dbo.pc_ship_plan_line_source src
               ON src.plan_date = l.plan_date
              AND src.nx_order_numb = l.nx_order_numb
              AND src.nx_rel_numb = l.nx_rel_numb
              AND src.nx_item_no = l.nx_item_no
        LEFT JOIN #pcr cr                                  -- staged feed (2026-09-28)
               ON cr.nx_order_numb = l.nx_order_numb
              AND cr.nx_rel_numb = l.nx_rel_numb
              AND cr.nx_item_no = l.nx_item_no
        LEFT JOIN #pcs fs
               ON fs.nx_order_numb = l.nx_order_numb
              AND fs.nx_rel_numb = l.nx_rel_numb
              AND fs.nx_item_no = l.nx_item_no
        LEFT JOIN dbo.pc_priority_reasons pr
               ON pr.reason_code = l.priority_reason
        WHERE l.plan_date = %s
        ORDER BY CASE l.line_status WHEN 'Unplaced' THEN 0 ELSE 1 END,
                 CASE l.tier WHEN 'Must' THEN 0 WHEN 'Next' THEN 1
                             ELSE 2 END,
                 l.position,
                 -- 104 (Sales, 2026-10-12): an order's lines sit together -
                 -- the order with the biggest single live line first, then
                 -- release and line (the line number as a NUMBER: nx_item_no is
                 -- text, so 110 would sort before 20). Same rule as sp_stamp_plan_work_orders.
                 MAX(CASE WHEN l.line_status IN ('Planned', 'Proposed', 'Confirmed', 'Unplaced')
                          THEN ISNULL(l.load_inches, 0) END) OVER (PARTITION BY l.nx_order_numb) DESC,
                 l.nx_order_numb, l.nx_rel_numb, TRY_CAST(LTRIM(RTRIM(l.nx_item_no)) AS INT), l.nx_item_no
    """, ('%,A05,%', '%,A04,%', _next_business_day(date.today() + timedelta(days=1)),
          plan_date))
    lines = []
    for r in cursor.fetchall():
      row = serialize_row(r)
      live = row.get('live_qty')                  # e21 balance due (display)
      ordered = row.get('live_ordered')           # e21 orditem.qty (planning qty, 066)
      planned = float(row['qty_planned'])
      is_split = (row.get('priority_note') or '').startswith('SPLIT ')   # 10-08
      if row['line_status'] in _ACTIVE_STATUSES:
        if ordered is None:
          row['drift'] = 'gone'          # nothing open — likely shipped
        elif is_split and planned < float(ordered) - 0.005:
          row['drift'] = None            # a planned quantity, by design
        elif abs(float(ordered) - planned) > 0.005:
          row['drift'] = f"{planned:g} → {float(ordered):g}"
        else:
          row['drift'] = None
      else:
        row['drift'] = None
      # 065 / C05: e21 balance due vs ordered disagree -> stale balance on
      # an e21 editing path (never a partial: e21 restates the line). The
      # Qty column shows "ordered (balance)"; PC re-accepts the line.
      if (live is not None and ordered is not None
          and abs(float(live) - float(ordered)) > 0.005):
        row['qty_warn'] = f"balance {float(live):g} vs ordered {float(ordered):g}"
      else:
        row['qty_warn'] = None
      row['line_label'] = _line_label(row['nx_order_numb'], row.get('nx_rel_numb'),
                                      row.get('nx_item_no'))
      row['customer'] = _customer_label(row.get('nx_cust_name'), row.get('nx_cust_city'),
                                        row.get('nx_cust_state'), row.get('shipto_code'))
      row['md'] = _md_parse_note(row.get('priority_note'))          # v1.6: multi-day facts for the row / star
      lines.append(row)

    planned_total = sum(float(l['load_inches']) for l in lines
                        if l['line_status'] in _COUNTS_AGAINST_CAP
                        and l.get('tier') != 'Stretch')
    stretch_total = sum(float(l['load_inches']) for l in lines
                        if l['line_status'] in _COUNTS_AGAINST_CAP
                        and l.get('tier') == 'Stretch')
    cursor.execute("""
        SELECT TOP 1 proposal_id, status, generated_at, agent_version,
               capacity_inches, proposed_inches, rationale,
               (SELECT COUNT(*) FROM dbo.pc_agent_proposal_line l
                WHERE l.proposal_id = p.proposal_id AND l.rank_no > 0) AS n_lines
        FROM dbo.pc_agent_proposal p
        WHERE plan_date = %s ORDER BY generated_at DESC
    """, (plan_date,))
    prop = cursor.fetchone()
    n_proposed_open = sum(1 for l in lines if l['line_status'] == 'Proposed')
    return {'status': 'success',
            'header': serialize_row(header) if header else None,
            'lines': lines,
            'planned_inches': round(planned_total, 1),
            'stretch_inches': round(stretch_total, 1),
            'capacity': _capacity_for(cursor, plan_date),
            'proposal': serialize_row(prop) if prop else None,
            'n_proposed_open': n_proposed_open}
  except Exception as e:
    return {'status': 'error', 'message': f'Plan load error: {e}'}
  finally:
    conn.close()


# =============================================================================
# MUTATIONS — one transaction each, event row inside it
# =============================================================================

def _fetch_candidate_rows(cursor, order, rel, item):
  """066: the ONE candidate row of an e21 line (order, release, item)."""
  cursor.execute("""
      SELECT * FROM dbo.vw_pc_ship_candidate_rows
      WHERE nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
  """, (order, rel, item))
  return cursor.fetchall()


def _aggregate(rows):
  """066: qty = the ORDERED quantity (e21 orditem.qty). One row per line
  now, but the shape is kept so the load math has one home."""
  qty = sum(float((r['nx_qty_ordered'] if r.get('nx_qty_ordered') is not None
                   else r['nx_qty_balance_due']) or 0) for r in rows)
  dates = [r['nx_dship_by_date'] or r['nx_hship_by_date'] for r in rows]
  dates = [d.date() if isinstance(d, datetime) else d for d in dates if d]
  return {
    'qty': qty,
    'sku': rows[0]['nx_sku'],
    'ship_by': min(dates) if dates else None,
    'shipto': rows[0]['nx_shipto_code'],
    'carrier': rows[0]['nx_carrier'],
    'minutes': sum(float(r['minutes_charged'] or 0) for r in rows),
    'surcharge': sum(float(r['surcharge_minutes'] or 0) for r in rows),
    'inches': sum(float(r['work_inches'] or 0) for r in rows),
  }


def _insert_source_rows(cursor, plan_date, order, rel, item, rows):
  """The plan-time snapshot of the e21 line (066: one row; qty_balance
  holds the ORDERED quantity the plan was built on)."""
  for r in rows:
    cursor.execute("""
        INSERT INTO dbo.pc_ship_plan_line_source
            (plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no,
             qty_balance, nx_attrib_val, element_class_used,
             class_was_defaulted, size_inches, minutes_charged)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (plan_date, order, r['nx_sku'], rel, item,
          (r['nx_qty_ordered'] if r.get('nx_qty_ordered') is not None
           else r['nx_qty_balance_due']),
          r['nx_attrib_val'],
          (r['element_class'] if r['element_class'] in
           ('Standard', 'Non-Standard') else 'n/a'),
          r['class_was_defaulted'], r['size_inches'], r['minutes_charged']))


def _delete_source_rows(cursor, plan_date, order, rel, item):
  cursor.execute("""
      DELETE FROM dbo.pc_ship_plan_line_source
      WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
  """, (plan_date, order, rel, item))


def _insert_line_and_source(cursor, plan_date, order, rel, item, rows, agg,
                            role='PC', actor=None, tier='Next', reason=None):
  cursor.execute("""
      INSERT INTO dbo.pc_ship_plan_line
          (plan_date, nx_order_numb, nx_rel_numb, nx_item_no, nx_sku,
           qty_planned, ship_by_date,
           shipto_code, carrier, load_inches, load_minutes,
           surcharge_minutes, line_status, added_by_role, created_by,
           tier, priority_reason)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Planned', %s, %s,
              %s, %s)
  """, (plan_date, order, rel, item, agg['sku'], agg['qty'], agg['ship_by'],
        agg['shipto'], agg['carrier'], agg['inches'], agg['minutes'],
        agg['surcharge'], role, actor or _current_actor(), tier, (reason or None)))
  _insert_source_rows(cursor, plan_date, order, rel, item, rows)


def _ensure_header(cursor, plan_date, actor):
  cursor.execute("SELECT plan_date FROM dbo.pc_ship_plan WHERE plan_date = %s",
                 (plan_date,))
  if not cursor.fetchone():
    weights = _get_active_weights(cursor)
    cursor.execute("""
        INSERT INTO dbo.pc_ship_plan (plan_date, weight_set_id, created_by)
        VALUES (%s, %s, %s)
    """, (plan_date, weights['weight_set_id'], actor))


# 2026-10-08 split guard: a line bigger than this share of the day's
# capacity is a multi-day job and gets a quantity, not the whole order.
_SPLIT_GUARD_FRACTION = 0.5


def _scale_agg(agg, qty):
  """A partial quantity of a line: load scales per piece (the per-line
  minutes are size x qty, so this is exact for the element work; the
  surcharge is scaled the same way and documented as such)."""
  full = float(agg['qty'] or 0)
  if full <= 0 or qty >= full:
    return dict(agg)
  f = float(qty) / full
  out = dict(agg)
  out['qty'] = float(qty)
  out['inches'] = round(float(agg['inches']) * f, 2)
  out['minutes'] = round(float(agg['minutes']) * f, 2)
  out['surcharge'] = round(float(agg['surcharge']) * f, 2)
  return out


def _split_note(qty, full):
  return f'SPLIT {qty:g} of {full:g}'


@anvil.server.callable
def ship_plan_add_line(plan_date, order, rel, item, tier='Next', reason=None,
                       override_note=None, qty=None):
  """Add one e21 line (order, release, item) to a day. The line is ALWAYS
  added — over-cap is reported back so the client runs the cap-hit dialog
  (snooze / unplaced / stretch / keep). Never a silent drop.
  override_note (075, 2026-09-18): NOT None means PC is adding a line the
  gates say cannot ship - today only X06 'No stock - due in', to pre-build
  the straining element while the body is on the water. The live gate
  codes are recorded in a GateOverride event (new_value) and the note is
  written to priority_note so the board shows PRE-BUILD on the line. The
  retro judges lines on WORKED_ON, so a pre-build SE starts closes as
  WORKED and one nobody touched earns the normal PC question.
  qty (2026-10-08, split guard): None = the whole ordered quantity, but a
  line bigger than _SPLIT_GUARD_FRACTION of the day's capacity is NOT
  added - the call returns status 'needs_split' with the numbers and the
  client asks PC for today's quantity. qty = a number plans that many
  pieces (load scaled per piece, 'SPLIT n of N' on the line); qty = 'all'
  adds the whole line regardless (PC's call, the cap-hit dialog follows)."""
  plan_date = _parse_date(plan_date)
  if tier not in ('Must', 'Next', 'Stretch'):
    return {'status': 'error', 'message': f'Bad tier {tier}.'}
  force_all = (qty == 'all')
  if force_all:
    qty = None
  if qty is not None:
    try:
      qty = float(qty)
    except Exception:
      return {'status': 'error', 'message': f'Bad quantity {qty!r}.'}
    if qty <= 0:
      return {'status': 'error', 'message': 'Quantity must be above zero.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    gate_codes = None
    if override_note is not None:
      cursor.execute("""
          SELECT gate_codes FROM dbo.vw_pc_order_flag_scores
          WHERE nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
            AND is_gated = 1
      """, (order, rel, item))
      grow = cursor.fetchone()
      gate_codes = (grow or {}).get('gate_codes')
      if not gate_codes:
        # the gate cleared between the popup and the click - a plain add
        override_note = None
    cursor.execute("""
        SELECT line_status, nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (plan_date, order, rel, item))
    existing = cursor.fetchone()
    label = _line_label(order, rel, item, existing['nx_sku'] if existing else None)
    # 2026-09-25: an UNPLACED line (cap-hit / material hold) stays visible on
    # the plan AND in the candidates by design - so + on the candidate must
    # re-place it, not answer 'already on this plan' (599024-001-10 today).
    if existing and existing['line_status'] not in ('Removed', 'Unplaced'):
      return {'status': 'error',
              'message': f'{label} is already on this plan '
              f"({existing['line_status']})."}
    was_unplaced = bool(existing and existing['line_status'] == 'Unplaced')

    rows = _fetch_candidate_rows(cursor, order, rel, item)
    if not rows:
      return {'status': 'error',
              'message': f'{label} has no open quantity in the feed '
              '(shipped, or already planned on another day).'}
    agg = _aggregate(rows)
    label = _line_label(order, rel, item, agg['sku'])
    full_qty = float(agg['qty'] or 0)
    split_note = None
    if qty is not None and qty < full_qty:
      split_note = _split_note(qty, full_qty)
      agg = _scale_agg(agg, qty)
    elif qty is None and not force_all and full_qty > 1:
      # ---- split guard (2026-10-08): stop and ask before adding -------
      cap_now = _capacity_for(cursor, plan_date)['capacity_inches_effective']
      if cap_now > 0 and float(agg['inches']) > _SPLIT_GUARD_FRACTION * cap_now:
        planned_now = _planned_inches(cursor, plan_date)
        per_piece = float(agg['inches']) / full_qty
        room = max(0.0, cap_now - planned_now)
        fits = int(room / per_piece) if per_piece > 0 else 0
        if fits < 1:                      # day already full: offer half a day
          fits = int((cap_now * _SPLIT_GUARD_FRACTION) / per_piece) if per_piece > 0 else 1
        step = 25 if full_qty >= 200 else (10 if full_qty >= 40 else 1)
        suggested = max(step if step < full_qty else 1, (fits // step) * step)
        suggested = min(suggested, int(full_qty) - 1) if full_qty > 1 else 1
        return {'status': 'needs_split', 'label': label, 'sku': agg['sku'],
                'qty': full_qty, 'line_inches': round(float(agg['inches']), 1),
                'per_piece_inches': round(per_piece, 2),
                'capacity_inches': round(cap_now, 1),
                'planned_inches': round(planned_now, 1),
                'suggested_qty': int(suggested)}
    _ensure_header(cursor, plan_date, actor)

    if existing:  # was Removed / Unplaced — revive rather than violate the PK
      _delete_source_rows(cursor, plan_date, order, rel, item)
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET nx_sku = %s, qty_planned = %s, ship_by_date = %s, shipto_code = %s,
              carrier = %s, load_inches = %s, load_minutes = %s,
              surcharge_minutes = %s, line_status = 'Planned',
              tier = %s, priority_reason = %s,
              snooze_to_date = NULL, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (agg['sku'], agg['qty'], agg['ship_by'], agg['shipto'], agg['carrier'],
            agg['inches'], agg['minutes'], agg['surcharge'],
            tier, (reason or None), actor,
            plan_date, order, rel, item))
      _insert_source_rows(cursor, plan_date, order, rel, item, rows)
    else:
      _insert_line_and_source(cursor, plan_date, order, rel, item, rows, agg,
                              actor=actor, tier=tier, reason=reason)

    if split_note:
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET priority_note = %s, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (split_note, actor, plan_date, order, rel, item))
    _log_event(cursor, plan_date, order, agg['sku'], 'Added',
               new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in "
               f"({tier})" + (f" {split_note}" if split_note else ""),
               actor=actor, note=('re-placed (was Unplaced)' if was_unplaced else None),
               rel=rel, item=item)
    if override_note is not None:
      # 075: the override is its own event (the Added event stays plain so
      # every add reads the same in the retro); the note goes on the line.
      why = (override_note or '').strip()
      # 099: X08 short stock overridden = a deliberate PARTIAL (what is on
      # the shelf); X06 = PRE-BUILD (element ahead of the body). A split
      # quantity keeps its 'SPLIT n of N' in front so the drift rule and
      # Refresh still recognise it.
      gset = set(gate_codes.split(','))
      kind = 'PARTIAL' if ('X08' in gset and 'X06' not in gset) else 'PRE-BUILD'
      board_note = ((split_note + ' - ') if split_note else '') \
      + kind + ' (gate ' + gate_codes.replace(',', ' ') + ' overridden)'
      if why:
        board_note += ': ' + why
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET priority_note = %s, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (board_note[:200], actor, plan_date, order, rel, item))
      _log_event(cursor, plan_date, order, agg['sku'], 'GateOverride',
                 new_value=gate_codes, actor=actor,
                 note=(why or ('partial - ship what is on the shelf' if kind == 'PARTIAL'
                               else 'pre-build straining element')), rel=rel, item=item)
    _bump_revision(cursor, plan_date, actor)

    planned = _planned_inches(cursor, plan_date)
    cap = _capacity_for(cursor, plan_date)['capacity_inches_effective']
    over = planned > cap
    suggested = _suggest_day(cursor, agg['inches'], plan_date) if over else None
    conn.commit()
    return {'status': 'success', 'planned_inches': round(planned, 1),
            'capacity_inches': round(cap, 1), 'over_capacity': over,
            'line_inches': round(agg['inches'], 1),
            'suggested_day': suggested, 'label': label,
            'gate_override': gate_codes}
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


def _set_priority_tx(cursor, plan_date, order, rel, item, tier, reason, note, actor,
                     event_note=None):
  """Tier / reason / note on ONE line inside the caller's transaction
  (2026-09-24: shared by the single-line and the whole-order callable).
  Returns (changed, error_message). Pass None for 'leave alone', '' to
  clear. Each change gets its own event (TierChange / ReasonSet)."""
  cursor.execute("""
      SELECT tier, priority_reason, priority_note, line_status, nx_sku
      FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
  """, (plan_date, order, rel, item))
  row = cursor.fetchone()
  if not row:
    return False, 'Line not found.'
  sku = row['nx_sku']
  if row['line_status'] == 'Removed':
    return False, 'Line is removed — revive it first (re-add the order).'

  changed = False
  if tier is not None and tier != row['tier']:
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET tier = %s, updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (tier, actor, plan_date, order, rel, item))
    _log_event(cursor, plan_date, order, sku, 'TierChange',
               old_value=row['tier'], new_value=tier, actor=actor,
               note=event_note, rel=rel, item=item)
    changed = True
  if reason is not None:
    new_reason = reason or None          # '' clears
    if new_reason != row['priority_reason']:
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET priority_reason = %s, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (new_reason, actor, plan_date, order, rel, item))
      _log_event(cursor, plan_date, order, sku, 'ReasonSet',
                 old_value=row['priority_reason'], new_value=new_reason,
                 actor=actor, note=event_note, rel=rel, item=item)
      changed = True
  if note is not None:
    new_note = (note or '').strip()[:200] or None
    pfx = _system_prefix(row['priority_note'])      # v1.4: 'SPLIT n of N' / 'SHELF DECISION' survive a hand edit
    if pfx and not (new_note or '').startswith(pfx[:5]):
      new_note = (pfx + ((' - ' + new_note) if new_note else ''))[:200]
    if new_note != row['priority_note']:
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET priority_note = %s, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (new_note, actor, plan_date, order, rel, item))
      _log_event(cursor, plan_date, order, sku, 'ReasonSet',
                 old_value=row['priority_note'], new_value=new_note,
                 actor=actor, note=('note edit' if not event_note
                                    else f'note edit; {event_note}'),
                 rel=rel, item=item)
      changed = True
  return changed, None


@anvil.server.callable
def ship_plan_set_priority(plan_date, order, rel, item, tier=None, reason=None,
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
    changed, err = _set_priority_tx(cursor, plan_date, order, rel, item,
                                    tier, reason, note, actor)
    if err:
      conn.rollback()
      return {'status': 'error', 'message': err}
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
def ship_plan_set_order_priority(plan_date, order, tier=None, reason=None,
                                 note=None):
  """2026-09-24 (Dave): the ★ dialog's 'ALL lines of the order' box - the
  same tier / reason / note on EVERY active line of a sales order on this
  day (Planned / Proposed / Confirmed; Snoozed and Removed are left alone),
  one transaction. Per-line TierChange / ReasonSet events carry the note
  'whole order' plus one OrderPriority summary (event type added by 086).
  Lines already at the requested values are unchanged and not counted."""
  plan_date = _parse_date(plan_date)
  if tier is not None and tier not in ('Must', 'Next', 'Stretch'):
    return {'status': 'error', 'message': f'Bad tier {tier}.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_rel_numb, nx_item_no, nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND line_status IN %s
        ORDER BY nx_rel_numb, nx_item_no
    """, (plan_date, order, _COUNTS_AGAINST_CAP))
    items = [(r['nx_rel_numb'], r['nx_item_no'], r['nx_sku']) for r in cursor.fetchall()]
    if not items:
      return {'status': 'error', 'message': f'{order} has no active lines on this plan.'}
    changed_lines, errors = [], []
    for rel, item, sku in items:
      ch, err = _set_priority_tx(cursor, plan_date, order, rel, item,
                                 tier, reason, note, actor, event_note='whole order')
      if err:
        errors.append(f'{_line_label(order, rel, item, sku)}: {err}')
      elif ch:
        changed_lines.append(_line_label(order, rel, item, sku))
    if errors:
      conn.rollback()
      return {'status': 'error', 'message': 'Nothing changed: ' + ' '.join(errors)}
    if not changed_lines:
      conn.rollback()
      return {'status': 'success', 'changed': False, 'n_lines': len(items),
              'changed_lines': []}
    what = ', '.join(x for x in (
      f'tier {tier}' if tier is not None else None,
      f'reason {reason or "(cleared)"}' if reason is not None else None,
      'note' if note is not None else None) if x)
    _log_event(cursor, plan_date, order, None, 'OrderPriority',
               new_value=f'{len(changed_lines)} of {len(items)} lines: {what}'[:200],
               actor=actor, note=((note or '').strip()[:200] or None))
    _bump_revision(cursor, plan_date, actor)
    planned = _planned_inches(cursor, plan_date)
    cap = _capacity_for(cursor, plan_date)['capacity_inches_effective']
    conn.commit()
    return {'status': 'success', 'changed': True, 'n_lines': len(items),
            'changed_lines': changed_lines,
            'planned_inches': round(planned, 1),
            'capacity_inches': round(cap, 1),
            'over_capacity': planned > cap}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Order priority failed: {e}'}
  finally:
    conn.close()


def _order_lines_for_add(cursor, order, due_date=None):
  """The open e21 lines +SO would add (066: rel, item)."""
  if due_date:
    cursor.execute("""
        SELECT nx_rel_numb, nx_item_no, nx_sku FROM dbo.vw_pc_ship_candidate_rows
        WHERE nx_order_numb = %s AND CAST(nx_dship_by_date AS DATE) = %s
        ORDER BY nx_rel_numb, nx_item_no
    """, (order, _parse_date(due_date)))
  else:
    cursor.execute("""
        SELECT nx_rel_numb, nx_item_no, nx_sku FROM dbo.vw_pc_ship_candidate_rows
        WHERE nx_order_numb = %s ORDER BY nx_rel_numb, nx_item_no
    """, (order,))
  return [(r['nx_rel_numb'], r['nx_item_no'], r['nx_sku']) for r in cursor.fetchall()]


def _order_add_checks(cursor, plan_date, order, items):
  """2026-10-13: what +SO is about to walk past. Gated lines (with the gate
  wording) and SKUs the order over-commits on the shelf, counting what is
  already on this day. Informational - the add goes ahead on PC's say-so."""
  gated, by_sku = [], {}
  for rel, item, sku in items:
    cursor.execute("""
        SELECT fs.gate_codes, c.nx_qty_ordered
        FROM dbo.vw_pc_ship_candidate_rows c
        LEFT JOIN dbo.vw_pc_order_flag_scores fs
               ON fs.nx_order_numb = c.nx_order_numb AND fs.nx_rel_numb = c.nx_rel_numb
              AND fs.nx_item_no = c.nx_item_no AND fs.is_gated = 1
        WHERE c.nx_order_numb = %s AND c.nx_rel_numb = %s AND c.nx_item_no = %s
    """, (order, rel, item))
    r = cursor.fetchone() or {}
    qty = float(r.get('nx_qty_ordered') or 0)
    key = (sku or '').strip()
    if key:
      by_sku.setdefault(key, {'sku': key, 'needed': 0.0, 'n_lines': 0})
      by_sku[key]['needed'] += qty
      by_sku[key]['n_lines'] += 1
    if r.get('gate_codes'):
      cursor.execute("""
          SELECT flag_code, note FROM dbo.vw_pc_order_flags
          WHERE nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s AND flag_code LIKE 'X%%'
          ORDER BY flag_code
      """, (order, rel, item))
      notes = cursor.fetchall()
      gated.append({'rel': rel, 'item': item, 'sku': sku,
                    'line': _line_label(order, rel, item, sku),
                    'gates': r['gate_codes'],
                    'note': '; '.join((n['note'] or '') for n in notes)[:300]})
  shelf = []
  for key, s in by_sku.items():
    cursor.execute("""
        SELECT i.on_hand, LTRIM(RTRIM(ISNULL(d.nx_item_type, ''))) AS item_type
        FROM dbo.sales_orders_due d
        LEFT JOIN dbo.inventory_current i ON i.sku = d.nx_sku AND i.cost_center = 'Starlite'
        WHERE d.nx_order_numb = %s AND LTRIM(RTRIM(d.nx_sku)) = %s
    """, (order, key))
    inv = cursor.fetchone() or {}
    if inv.get('on_hand') is None or inv.get('item_type') == 'N':
      continue
    cursor.execute("""
        SELECT COALESCE(SUM(qty_planned), 0) AS q FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND LTRIM(RTRIM(nx_sku)) = %s AND nx_order_numb <> %s
          AND line_status IN %s
    """, (plan_date, key, order, _COUNTS_AGAINST_CAP))
    planned = float((cursor.fetchone() or {}).get('q') or 0)
    on_hand = float(inv['on_hand'] or 0)
    if s['needed'] + planned > on_hand + 1e-9:
      shelf.append({'sku': key, 'needed': s['needed'], 'n_lines': s['n_lines'],
                    'on_hand': on_hand, 'planned_today': planned})
  return gated, shelf


@anvil.server.callable
def ship_plan_add_order_preview(plan_date, order, due_date=None):
  """What +SO is about to do (2026-10-13): the lines, the gated ones with
  their gate wording, and any SKU the order over-commits on the shelf.
  The client shows ONE confirm when either list is non-empty."""
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    items = _order_lines_for_add(cursor, order, due_date)
    gated, shelf = _order_add_checks(cursor, plan_date, order, items)
    return {'status': 'success', 'n_lines': len(items), 'gated': gated, 'shelf': shelf}
  except Exception as e:
    return {'status': 'error', 'message': f'Preview failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_add_order(plan_date, order, tier='Next', reason=None,
                        due_date=None, acknowledged=False):
  """Add EVERY open e21 line of a sales order to the day — one transaction.
  Per-line Added events plus one OrderAdded summary; a single cap check
  at the end (the client runs one dialog for the whole order, not one
  per line). Lines already on the plan are skipped, not errors.
  due_date (2026-09-13, whole-order rule): only the lines with that
  DETAIL ship-by date - the publish check's "Add the rest".
  066: iterates e21 lines (rel, item), not SKUs."""
  plan_date = _parse_date(plan_date)
  if tier not in ('Must', 'Next', 'Stretch'):
    return {'status': 'error', 'message': f'Bad tier {tier}.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    items = _order_lines_for_add(cursor, order, due_date)
    if not items:
      return {'status': 'error',
              'message': f'{order} has no open quantity in the feed '
              '(shipped, or already planned on another day).'}

    _ensure_header(cursor, plan_date, actor)
    # 2026-10-13: +SO still adds every line (PC's decision) - but a gated
    # line and an over-committed shelf are RECORDED on the line and in the
    # events, like a single-line 'Add anyway', so the retro knows it was
    # deliberate (611796-001-10 X08 came in silently at 08:45 today).
    gated_now, shelf_now = _order_add_checks(cursor, plan_date, order, items)
    gated_by_key = {(g['rel'], g['item']): g for g in gated_now}
    shelf_by_sku = {s['sku']: s for s in shelf_now}
    added, skipped, order_inches = [], [], 0.0
    for rel, item, sku in items:
      label = _line_label(order, rel, item, sku)
      cursor.execute("""
          SELECT line_status FROM dbo.pc_ship_plan_line
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (plan_date, order, rel, item))
      existing = cursor.fetchone()
      if existing and existing['line_status'] not in ('Removed', 'Unplaced'):   # 2026-09-25
        skipped.append(label)
        continue
      rows = _fetch_candidate_rows(cursor, order, rel, item)
      if not rows:
        skipped.append(label)
        continue
      agg = _aggregate(rows)
      if existing:  # Removed / Unplaced — revive: clear then reinsert cleanly
        _delete_source_rows(cursor, plan_date, order, rel, item)
        cursor.execute("""
            DELETE FROM dbo.pc_ship_plan_line
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
        """, (plan_date, order, rel, item))
      _insert_line_and_source(cursor, plan_date, order, rel, item, rows, agg,
                              actor=actor, tier=tier, reason=reason)
      _log_event(cursor, plan_date, order, agg['sku'], 'Added',
                 new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in "
                 f"({tier})",
                 actor=actor, note='whole order', rel=rel, item=item)
      added.append(label)
      order_inches += agg['inches']
      # the override / shelf record (board note <= 200, event note <= 500)
      g = gated_by_key.get((rel, item))
      s = shelf_by_sku.get((agg['sku'] or '').strip())
      bits = []
      if g:
        gset = set((g['gates'] or '').split(','))
        kind = 'PARTIAL' if (gset == {'X08'}) else ('PRE-BUILD' if gset == {'X06'} else 'OVERRIDE')
        bits.append(f"{kind} (gate {(g['gates'] or '').replace(',', ' ')} overridden via +SO)")
        _log_event(cursor, plan_date, order, agg['sku'], 'GateOverride',
                   new_value=g['gates'], actor=actor,
                   note=('added with the whole order (+SO)'
                         + ('; PC acknowledged the gates' if acknowledged else '')
                         + ': ' + (g['note'] or ''))[:500], rel=rel, item=item)
      if s:
        bits.append(f"SHELF: order needs {s['needed']:g} of {s['sku']} on {s['n_lines']} lines, "
                    f"{s['on_hand']:g} on hand"
                    + (f" ({s['planned_today']:g} already planned today)" if s['planned_today'] else '')
                    + " (+SO)")
      if bits:
        cursor.execute("""
            UPDATE dbo.pc_ship_plan_line
            SET priority_note = %s, updated_by = %s, updated_at = GETDATE()
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
        """, (' - '.join(bits)[:200], actor, plan_date, order, rel, item))

    if not added:
      conn.rollback()
      return {'status': 'error',
              'message': f'Every open line of {order} is already on this '
              'plan — nothing to add.'}

    _log_event(cursor, plan_date, order, None, 'OrderAdded',
               new_value=f'{len(added)} lines / {order_inches:.1f} in '
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
            'suggested_day': suggested,
            'n_gated': len(gated_now), 'n_shelf': len(shelf_now)}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Add order failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_partial_orders(plan_date):
  """WHOLE-ORDER RULE (Dave, 2026-09-13) - the plan invariant, checked at
  Publish for whoever built the plan (agent or person): every order on
  the day whose open lines with the SAME detail ship-by date are not all
  on it. Returns the missing lines per (order, due date) so the client
  can offer 'Add the rest'. Lines already planned on another day or
  snoozed are reported with where they went (PC decided; not auto-added).
  Hidden lines (charges, services - pc_plan_exclusions) are not lines.
  066: at e21-line grain (order, release, item)."""
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    # The candidate-rows view carries a per-row size parser; pin the few
    # orders on this plan into a temp table once (pooled connection: drop
    # it before and after).
    cursor.execute("IF OBJECT_ID('tempdb..#cr') IS NOT NULL DROP TABLE #cr")
    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, nx_sku_desc, bucket,
               nx_qty_ordered, work_inches,
               CAST(nx_dship_by_date AS DATE) AS due_date
        INTO #cr
        FROM dbo.vw_pc_ship_candidate_rows
        WHERE nx_order_numb IN (SELECT nx_order_numb FROM dbo.pc_ship_plan_line
                                WHERE plan_date = %s
                                  AND line_status IN ('Planned', 'Proposed', 'Confirmed'))
    """, (plan_date,))
    cursor.execute("""
        SELECT r.nx_order_numb, r.due_date, r.nx_rel_numb, r.nx_item_no, r.nx_sku,
               r.nx_sku_desc AS sku_desc, r.bucket,
               r.nx_qty_ordered AS qty, r.work_inches AS inches,
               (SELECT TOP 1 CONVERT(VARCHAR(10), ISNULL(x.snooze_to_date, x.plan_date), 120)
                             + ' ' + x.line_status
                FROM dbo.pc_ship_plan_line x
                WHERE x.nx_order_numb = r.nx_order_numb AND x.nx_rel_numb = r.nx_rel_numb
                  AND x.nx_item_no = r.nx_item_no
                  AND x.plan_date <> %s
                  AND ((x.plan_date >= CAST(GETDATE() AS DATE)
                        AND x.line_status IN ('Planned', 'Proposed', 'Confirmed'))
                    OR (x.line_status = 'Snoozed'
                        AND ISNULL(x.snooze_to_date, x.plan_date) >= CAST(GETDATE() AS DATE)))
                ORDER BY x.plan_date DESC) AS elsewhere,
               (SELECT MIN(CASE p.tier WHEN 'Must' THEN 0 WHEN 'Next' THEN 1 ELSE 2 END)
                FROM dbo.pc_ship_plan_line p
                JOIN #cr pr ON pr.nx_order_numb = p.nx_order_numb
                           AND pr.nx_rel_numb = p.nx_rel_numb AND pr.nx_item_no = p.nx_item_no
                WHERE p.plan_date = %s AND p.nx_order_numb = r.nx_order_numb
                  AND p.line_status IN ('Planned', 'Proposed', 'Confirmed')
                  AND pr.due_date = r.due_date) AS tier_rank
        FROM #cr r
        WHERE EXISTS (SELECT 1
                      FROM dbo.pc_ship_plan_line p
                      JOIN #cr pr ON pr.nx_order_numb = p.nx_order_numb
                                 AND pr.nx_rel_numb = p.nx_rel_numb AND pr.nx_item_no = p.nx_item_no
                      WHERE p.plan_date = %s AND p.nx_order_numb = r.nx_order_numb
                        AND p.line_status IN ('Planned', 'Proposed', 'Confirmed')
                        AND pr.due_date = r.due_date)
          AND NOT EXISTS (SELECT 1 FROM dbo.pc_ship_plan_line p2
                          WHERE p2.plan_date = %s AND p2.nx_order_numb = r.nx_order_numb
                            AND p2.nx_rel_numb = r.nx_rel_numb AND p2.nx_item_no = r.nx_item_no
                            AND p2.line_status IN ('Planned', 'Proposed', 'Confirmed'))
        ORDER BY r.nx_order_numb, r.due_date, r.nx_rel_numb, r.nx_item_no
    """, (plan_date, plan_date, plan_date, plan_date))
    rows = cursor.fetchall()
    cursor.execute("IF OBJECT_ID('tempdb..#cr') IS NOT NULL DROP TABLE #cr")
    by_group = {}
    for r in rows:
      k = (r['nx_order_numb'], str(r['due_date'])[:10])
      g = by_group.setdefault(k, {'order': k[0], 'due_date': k[1], 'lines': [],
                                  'missing_inches': 0.0, 'tier': 'Next'})
      g['lines'].append({'rel': r['nx_rel_numb'], 'item': r['nx_item_no'],
                         'label': _line_label(k[0], r['nx_rel_numb'], r['nx_item_no']),
                         'sku': r['nx_sku'], 'desc': r['sku_desc'], 'bucket': r['bucket'],
                         'qty': float(r['qty'] or 0), 'inches': float(r['inches'] or 0),
                         'elsewhere': r['elsewhere']})
      g['missing_inches'] += float(r['inches'] or 0)
      g['tier'] = {0: 'Must', 1: 'Next'}.get(r['tier_rank'], 'Stretch')
    groups = list(by_group.values())
    for g in groups:
      g['missing_inches'] = round(g['missing_inches'], 1)
      g['n_addable'] = sum(1 for l in g['lines'] if not l['elsewhere'])
    return {'status': 'success', 'plan_date': plan_date.isoformat(),
            'n_partial': len(groups), 'groups': groups}
  except Exception as e:
    try:
      conn.rollback()
      conn.cursor().execute("IF OBJECT_ID('tempdb..#cr') IS NOT NULL DROP TABLE #cr")
    except Exception:
      pass
    return {'status': 'error', 'message': f'Partial-order check failed: {e}'}
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
        SELECT nx_rel_numb, nx_item_no, nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s
          AND line_status <> 'Removed'
    """, (plan_date, order))
    items = [(r['nx_rel_numb'], r['nx_item_no'], r['nx_sku']) for r in cursor.fetchall()]
    if not items:
      return {'status': 'error',
              'message': f'{order} has no active lines on this plan.'}
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Removed', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s
          AND line_status <> 'Removed'
    """, (actor, plan_date, order))
    for rel, item, sku in items:
      _log_event(cursor, plan_date, order, sku, 'Removed',
                 actor=actor, note=note or 'whole order', rel=rel, item=item)
    _log_event(cursor, plan_date, order, None, 'OrderRemoved',
               new_value=f'{len(items)} lines', actor=actor, note=note)
    _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success',
            'removed': [_line_label(order, rel, item, sku) for rel, item, sku in items]}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Remove order failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_remove_line(plan_date, order, rel, item, note=None):
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND line_status <> 'Removed'
    """, (plan_date, order, rel, item))
    hit = cursor.fetchone()
    if not hit:
      return {'status': 'error', 'message': 'Line not found (or already removed).'}
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Removed', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND line_status <> 'Removed'
    """, (_current_actor(), plan_date, order, rel, item))
    _log_event(cursor, plan_date, order, hit['nx_sku'], 'Removed', note=note,
               rel=rel, item=item)
    _bump_revision(cursor, plan_date, None)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Remove failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_mark_unplaced(plan_date, order, rel, item, note=None):
  """The explicit queue (DECISION §4) — visible on the day PC tried."""
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (plan_date, order, rel, item))
    hit = cursor.fetchone()
    if not hit:
      return {'status': 'error', 'message': 'Line not found.'}
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Unplaced', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (_current_actor(), plan_date, order, rel, item))
    _log_event(cursor, plan_date, order, hit['nx_sku'], 'Unplaced', note=note,
               rel=rel, item=item)
    _bump_revision(cursor, plan_date, None)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Unplace failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_release_hold(order, rel, item, note=None):
  """071: PC releases a material hold (X05) from the candidate flag
  popup - stock arrived, or the answer was wrong. The line re-enters
  ranking on the next refresh."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        EXEC dbo.sp_pc_release_line_hold @order = %s, @rel = %s, @item = %s,
             @released_by = %s, @note = %s
    """, (order, rel, item, _current_actor(), (note or None)))
    r = cursor.fetchone() or {}
    conn.commit()
    if not int(r.get('released') or 0):
      return {'status': 'error', 'message': 'No live hold on that line.'}
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Release failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def _snooze_line_tx(cursor, plan_date, order, rel, item, to_date, actor, note=None):
  """The per-line snooze, inside the caller's transaction (no commit):
  checks, marks this day's line Snoozed, re-captures the feed rows fresh
  for the new day and inserts the future line. Returns (ok, message).
  2026-09-16: shared by the single-line and the whole-order snooze. The
  feed check runs BEFORE the UPDATE so a skipped line leaves no mark."""
  cursor.execute("""
      SELECT line_status FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
  """, (to_date, order, rel, item))
  if cursor.fetchone():
    return False, f'{_line_label(order, rel, item)} is already on {to_date}.'
  cursor.execute("""
      SELECT tier, priority_reason, nx_sku FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
  """, (plan_date, order, rel, item))
  src = cursor.fetchone() or {}
  rows = _fetch_candidate_rows(cursor, order, rel, item)
  if not rows:
    return False, (f'{_line_label(order, rel, item)}: no open quantity left in the '
                   'feed - nothing to snooze forward.')
  cursor.execute("""
      UPDATE dbo.pc_ship_plan_line
      SET line_status = 'Snoozed', snooze_to_date = %s,
          updated_by = %s, updated_at = GETDATE()
      WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
        AND line_status IN %s
  """, (to_date, actor, plan_date, order, rel, item, _ACTIVE_STATUSES))
  if cursor.rowcount == 0:
    return False, f'{_line_label(order, rel, item)}: not found or not active.'
  _log_event(cursor, plan_date, order, src.get('nx_sku'), 'Snoozed',
             new_value=to_date.isoformat(), actor=actor, rel=rel, item=item, note=note)
  agg = _aggregate(rows)
  _ensure_header(cursor, to_date, actor)
  _insert_line_and_source(cursor, to_date, order, rel, item, rows, agg,
                          actor=actor, tier=src.get('tier') or 'Next',
                          reason=src.get('priority_reason'))
  _log_event(cursor, to_date, order, agg['sku'], 'Added',
             new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in",
             actor=actor, note=f'snoozed from {plan_date}', rel=rel, item=item)
  return True, None


@anvil.server.callable
def ship_plan_snooze_line(plan_date, order, rel, item, to_date):
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
    ok, msg = _snooze_line_tx(cursor, plan_date, order, rel, item, to_date, actor)
    if not ok:
      conn.rollback()
      return {'status': 'error', 'message': msg}
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
def ship_plan_snooze_order(plan_date, order, to_date):
  """2026-09-16 (Dave, after the first snooze in the PC/Sales meeting):
  snooze EVERY line of a sales order that is on this day's plan
  (Planned / Proposed / Confirmed) to one later day - one transaction,
  per-line Snoozed + Added events, one OrderSnoozed summary. A line the
  feed no longer has open, or that already sits on the target day, is
  skipped and reported; the rest move. Nothing moves if nothing can."""
  plan_date = _parse_date(plan_date)
  to_date = _parse_date(to_date)
  if to_date <= plan_date:
    return {'status': 'error', 'message': 'Snooze target must be a later day.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_rel_numb, nx_item_no, nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND line_status IN %s
        ORDER BY nx_rel_numb, nx_item_no
    """, (plan_date, order, _COUNTS_AGAINST_CAP))
    items = [(r['nx_rel_numb'], r['nx_item_no'], r['nx_sku']) for r in cursor.fetchall()]
    if not items:
      return {'status': 'error', 'message': f'{order} has no active lines on this plan.'}
    moved, skipped = [], []
    for rel, item, sku in items:
      ok, msg = _snooze_line_tx(cursor, plan_date, order, rel, item, to_date, actor,
                                note='whole order')
      (moved if ok else skipped).append(_line_label(order, rel, item, sku) if ok else msg)
    if not moved:
      conn.rollback()
      return {'status': 'error', 'message': 'Nothing snoozed: ' + ' '.join(skipped)}
    _log_event(cursor, plan_date, order, None, 'OrderSnoozed',
               new_value=f'{len(moved)} lines -> {to_date.isoformat()}', actor=actor,
               note=(('skipped: ' + ' '.join(skipped))[:500] if skipped else None))
    _bump_revision(cursor, plan_date, actor)
    _bump_revision(cursor, to_date, actor)
    conn.commit()
    return {'status': 'success', 'to_date': to_date.isoformat(),
            'moved': moved, 'skipped': skipped}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Snooze order failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_recalc_line(plan_date, order, rel, item):
  """THE drift-rule writer — the only path that re-totals a plan line.
  Replaces the source snapshot and logs Recalc with old/new."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT qty_planned, load_inches, priority_note FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND line_status IN %s
    """, (plan_date, order, rel, item, _ACTIVE_STATUSES))
    old = cursor.fetchone()
    if not old:
      return {'status': 'error', 'message': 'Line not found or not active.'}

    rows = _fetch_candidate_rows(cursor, order, rel, item)
    if not rows:
      return {'status': 'error',
              'message': 'No open quantity in the feed — this line likely '
              'shipped. Remove it rather than refreshing.'}
    agg = _aggregate(rows)
    # 10-08: a split line keeps its planned quantity; only the per-piece
    # load is refreshed (the note is restated against the live ordered qty)
    if (old.get('priority_note') or '').startswith('SPLIT '):
      keep = float(old['qty_planned'])
      if keep < float(agg['qty'] or 0):
        agg = _scale_agg(agg, keep)
        cursor.execute("""
            UPDATE dbo.pc_ship_plan_line SET priority_note = %s
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
        """, (_split_note(keep, float(sum(float(r['nx_qty_ordered'] if r.get('nx_qty_ordered') is not None
                                                else r['nx_qty_balance_due']) for r in rows)))
              + _md_clause(old.get('priority_note')),        # v1.5: the day clause survives a Refresh
              plan_date, order, rel, item))

    _delete_source_rows(cursor, plan_date, order, rel, item)
    _insert_source_rows(cursor, plan_date, order, rel, item, rows)
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET nx_sku = %s, qty_planned = %s, ship_by_date = %s, load_inches = %s,
            load_minutes = %s, surcharge_minutes = %s,
            updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (agg['sku'], agg['qty'], agg['ship_by'], agg['inches'], agg['minutes'],
          agg['surcharge'], actor, plan_date, order, rel, item))

    old_txt = f"{float(old['qty_planned']):g} pcs / {float(old['load_inches']):.1f} in"
    new_txt = f"{agg['qty']:g} pcs / {agg['inches']:.1f} in"
    _log_event(cursor, plan_date, order, agg['sku'], 'Recalc',
               old_value=old_txt, new_value=new_txt, actor=actor, rel=rel, item=item)
    _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success', 'old': old_txt, 'new': new_txt}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Refresh failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def _snapshot_flags(cursor, plan_date, revision_no, weight_set_id):
  """Migration 051: freeze the WHOLE candidate pool (planned + not) with
  its codes, gate state, score and per-flag weights AS OF NOW. Flags are
  computed at read time and never stored, so this is the only record of
  what the ranking looked like when the plan went out — the agent's
  step-12 backtests replay from it. Idempotent per (plan_date, revision)."""
  cursor.execute("""
      INSERT INTO dbo.pc_plan_flag_snapshot
          (plan_date, revision_no, nx_order_numb, nx_rel_numb, nx_item_no, nx_sku,
           was_planned, tier, is_gated, gate_codes, priority_score, flag_codes,
           ship_by_date, qty_open, load_inches, capacity_weight_set_id)
      SELECT %s, %s, p.nx_order_numb, p.nx_rel_numb, p.nx_item_no, p.nx_sku,
             p.was_planned, p.tier,
             ISNULL(fs.is_gated, 0), fs.gate_codes,
             ISNULL(fs.priority_score, 0), fs.flag_codes,
             p.ship_by_date, p.qty_open, p.load_inches, %s
      FROM (
          SELECT l.nx_order_numb, l.nx_rel_numb, l.nx_item_no, l.nx_sku,
                 1 AS was_planned, l.tier,
                 l.ship_by_date, l.qty_planned AS qty_open, l.load_inches
          FROM dbo.pc_ship_plan_line l
          WHERE l.plan_date = %s
            AND l.line_status IN ('Planned','Proposed','Confirmed')
          UNION ALL
          SELECT c.nx_order_numb, c.nx_rel_numb, c.nx_item_no, c.nx_sku, 0, NULL,
                 c.ship_by_date, c.qty_open, c.load_inches
          FROM dbo.vw_pc_ship_candidates c
          WHERE NOT EXISTS (
              SELECT 1 FROM dbo.pc_ship_plan_line l2
              WHERE l2.plan_date = %s
                AND l2.nx_order_numb = c.nx_order_numb
                AND l2.nx_rel_numb = c.nx_rel_numb
                AND l2.nx_item_no = c.nx_item_no
                AND l2.line_status IN ('Planned','Proposed','Confirmed'))
      ) p
      LEFT JOIN dbo.vw_pc_order_flag_scores fs
             ON fs.nx_order_numb = p.nx_order_numb AND fs.nx_rel_numb = p.nx_rel_numb
            AND fs.nx_item_no = p.nx_item_no
      WHERE NOT EXISTS (
          SELECT 1 FROM dbo.pc_plan_flag_snapshot s
          WHERE s.plan_date = %s AND s.revision_no = %s
            AND s.nx_order_numb = p.nx_order_numb AND s.nx_rel_numb = p.nx_rel_numb
            AND s.nx_item_no = p.nx_item_no)
  """, (plan_date, revision_no, weight_set_id, plan_date, plan_date,
        plan_date, revision_no))
  n_rows = cursor.rowcount
  cursor.execute("""
      INSERT INTO dbo.pc_plan_flag_snapshot_detail
          (snapshot_id, flag_code, weight_at_time, note)
      SELECT s.snapshot_id, f.flag_code, f.weight, LEFT(f.note, 500)
      FROM dbo.pc_plan_flag_snapshot s
      JOIN dbo.vw_pc_order_flags f
        ON f.nx_order_numb = s.nx_order_numb AND f.nx_rel_numb = s.nx_rel_numb
       AND f.nx_item_no = s.nx_item_no
      WHERE s.plan_date = %s AND s.revision_no = %s
        AND NOT EXISTS (
            SELECT 1 FROM dbo.pc_plan_flag_snapshot_detail d
            WHERE d.snapshot_id = s.snapshot_id AND d.flag_code = f.flag_code)
  """, (plan_date, revision_no))
  return n_rows


@anvil.server.callable
def ship_plan_publish(plan_date, partial_ack=None):
  """Stamp capacity (the number sales saw) and flip Draft -> Published.
  The plan stays editable after — real life happens; edits keep logging.
  051: after the publish commits, the flag snapshot is taken in its OWN
  transaction — a snapshot problem must never un-publish a plan; it comes
  back as snapshot_warning for the client to show.
  partial_ack (2026-09-13): the partial orders the publisher chose to
  publish anyway, logged as one PartialAck event per order so the retro
  can tell 'knew' from 'missed'."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cap = _capacity_for(cursor, plan_date)
    for g in (partial_ack or []):
      try:
        _log_event(cursor, plan_date, g.get('order'), None, 'PartialAck',
                   new_value=f"{len(g.get('lines') or [])} lines left off, "
                   f"{float(g.get('missing_inches') or 0):,.0f} in",
                   actor=actor,
                   note='published anyway; due ' + str(g.get('due_date') or '') + ': '
                   + ', '.join((l.get('label') or l.get('sku') or '')
                               for l in (g.get('lines') or []))[:400])
      except Exception:
        pass                      # the acknowledgement must never block a publish
    cursor.execute("""
        UPDATE dbo.pc_ship_plan
        SET plan_status = 'Published', capacity_inches = %s,
            capacity_detail = %s, published_by = %s, published_at = GETDATE()
        WHERE plan_date = %s
    """, (cap['capacity_inches_effective'], cap['capacity_detail'][:200],
          actor, plan_date))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'No plan exists for that day yet.'}
    shelf_undecided = _drop_undecided_shelf_lines(cursor, plan_date, actor)   # v1.4
    proposal_outcome = _settle_proposal_outcomes(cursor, plan_date, actor)
    _log_event(cursor, plan_date, None, None, 'Published',
               new_value=f"capacity {cap['capacity_inches_effective']:.0f} in",
               actor=actor, note=cap['capacity_detail'][:500])
    conn.commit()
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Publish failed: {e}'}
  finally:
    conn.close()
  # --- flag snapshot (051), separate transaction --------------------
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("SELECT revision_no, weight_set_id FROM dbo.pc_ship_plan "
                   "WHERE plan_date = %s", (plan_date,))
    row = cursor.fetchone() or {}
    n = _snapshot_flags(cursor, plan_date, int(row.get('revision_no') or 0),
                        row.get('weight_set_id'))
    conn.commit()
    result = {'status': 'success', 'capacity': cap, 'snapshot_rows': n,
              'proposal_outcome': proposal_outcome, 'shelf_undecided': shelf_undecided}
  except Exception as e:
    conn.rollback()
    result = {'status': 'success', 'capacity': cap,
              'proposal_outcome': proposal_outcome,
              'snapshot_warning': f'Published, but the flag snapshot failed: {e}'}
  finally:
    conn.close()
  # --- work-order stamp (056), separate transaction, best effort -----
  conn = get_connection()
  try:
    cursor = conn.cursor()
    stamp = _stamp_work_orders(cursor, plan_date)
    conn.commit()
    if stamp is not None:
      result['wo_stamp'] = stamp
  except Exception as e:
    conn.rollback()
    result['stamp_warning'] = f'Published, but the work-order stamp failed: {e}'
  finally:
    conn.close()
  return result


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
        SELECT TOP %d event_id, plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no,
               event_type, old_value, new_value, actor, event_time, note
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


# =============================================================================
# AI AGENT — PROPOSAL ENGINE  (Operating Cycle steps 2-3; migrations 051/052)
# =============================================================================
# The agent is "just another proposer": it adds the day's picks as Proposed
# lines (proposed_by='agent', added_by_role='Agent') that PC confirms or
# drops in the pane they already use. The pick is DETERMINISTIC — score
# order, tier rule, fill to AGENT_FILL_PCT of capacity, then a Stretch tail —
# so it is explainable line by line. The model (titan-ai-service) never
# chooses lines; it writes the narrative on top (pc_agent_proposal.rationale)
# and sends the SMS. v1 authority: recommend, record, ask. Nothing here
# publishes, approves, or touches e21.

_AGENT_ACTOR = 'agent'
_AGENT_VERSION = 'v1.7.1' # v1.7.1 (page, step 3): multiday_get fixed (remaining snapshot, projection starts after the last planned day, roster estimate) + the grid; v1.7 (multi-day page, step 2): share = assemblers planned x one assembler-day, pool-capped (pc_multiday_plan, 121); PC share override and done-by-PC; v1.6.3: a line on today's plan does not block a future day; v1.6.2: only ASSEMBLY taps are assembly facts (OP codes are the Screen room kiosk); dry run sees today's lines; set_qty why; v1.6.1: a hand edit keeps the whole multi-day clause (assemblers, partials rule, pause); v1.6 (step 3): partials mode and pause on the star, multi-day on the board and the candidates; v1.5.3: the pool taker is the order that took inches; v1.5.2: a tablet-built line rides at 0 in, no check-the-floor lot; v1.5.1: the tablet (Complete / Start / Count) is the fact for multi-day lines; v1.5: multi-day orders at a day-sized share; v1.4.1: Stretch fits the buffer together; v1.4: shelf decisions ride at zero; v1.3: two orders, one shelf; v1.2: planned-short rule

_DUE_IN_RE = re.compile(r'^Due in (\d{4}-\d{2}-\d{2})')


def _due_in_eta(line_notes):
  """The ETA in a line's X06 note - the view's contract (075) is that the
  note ALWAYS starts 'Due in <yyyy-mm-dd>' or 'Due in (no ETA on PO)'.
  None when there is no X06, no ETA, or the text is not a date."""
  for n in line_notes or []:
    if (n.get('flag_code') or '') == 'X06':
      m = _DUE_IN_RE.match(n.get('note') or '')
      if m:
        try:
          return date.fromisoformat(m.group(1))
        except ValueError:
          return None
  return None


def _short_clause(m):
  """'608467-001-190 / SKU X02 no stock' - one gated sibling left off."""
  first = (m.get('short_note') or '').split(' - ')[0].split(':')[0].split(' (')[0].strip()
  return (f"{_line_label(m['nx_order_numb'], m['nx_rel_numb'], m['nx_item_no'], m['nx_sku'])} "
          f"{(m.get('gate_codes') or '').replace(',', ' ')}"
          + (f' {first.lower()}' if first else '')).strip()


def _agent_params(cursor):
  p = _get_params(cursor)
  def _f(k, d):
    try:
      return float(p.get(k, d))
    except (TypeError, ValueError):
      return float(d)
  return {'enabled': str(p.get('AGENT_ENABLED', '1')).strip() == '1',
          'fill_pct': max(0.0, min(_f('AGENT_FILL_PCT', 80), 100.0)) / 100.0,
          'stretch_lines': max(0, int(_f('AGENT_STRETCH_LINES', 5))),
          'horizon_days': max(0, int(_f('PLAN_HORIZON_DAYS', 3))),
          # zero-touch lines cost no capacity, so first-fit would propose
          # EVERY one of them (170 in the horizon on 2026-09-06). The plan
          # measures assembly; pick-and-ship stays off it unless switched on.
          'include_zero_touch': str(p.get('AGENT_INCLUDE_ZERO_TOUCH', '0')).strip() == '1',
          # a single line bigger than this share of the fill target is a
          # SPLIT decision, not a proposal (first run: one 1,631-in line
          # took 82% of the day).
          'max_line_pct': max(1.0, min(_f('AGENT_MAX_LINE_PCT', 50), 100.0)) / 100.0,
          # v1.5 (decisions 2026-10-25): the day's multi-day POOL as a share
          # of the fill target. An order over the max-line test is proposed
          # at a day-sized share out of this pool instead of being left as
          # a SPLIT decision. 0 = off (v1.4 behaviour). Migration 116.
          'multiday_share_pct': max(0.0, min(_f('AGENT_MULTIDAY_SHARE_PCT', 40), 100.0)) / 100.0}


def _next_business_day(d=None):
  d = d or date.today()
  while d.weekday() >= 5:
    d += timedelta(days=1)
  return d


def _horizon_end(days):
  """Same BUSINESS-day walk as the candidates pane (2026-08-27 lesson)."""
  end, added = date.today(), 0
  while added < int(days):
    end += timedelta(days=1)
    if end.weekday() < 5:
      added += 1
  return end


_REASON_WORDS = {   # short words for the reason line; notes carry detail
  'A01': 'late', 'A02': 'due now', 'A03': 'LD/freight', 'A04': 'rush',
  'A05': 'expedited', 'A06': 'must start', 'A07': 'carrier cutoff',
  'B01': 'strategic', 'B02': 'high $', 'B04': 'completes order',
  'B05': 'consolidates', 'B06': 'new customer', 'B07': 'bumped before',
  'E01': 'exception',
}


def _agent_reason(c, tier, notes):
  """One line PC reads on the row: tier, why (flag notes), load."""
  why = []
  for n in notes:
    code = n['flag_code']
    if code[:1] in ('A', 'B', 'E'):
      why.append(n['note'] or _REASON_WORDS.get(code, code))
  if not why:
    why = ['within horizon, by score']
  txt = f"{tier}: " + '; '.join(why)
  txt += f" — {float(c.get('load_inches') or 0):,.1f} in, score {int(c.get('priority_score') or 0)}"
  return txt[:500]


_SHELF_DECISION_PREFIX = 'SHELF DECISION:'


def _shelf_decision_note(c):
  """v1.4 board note for a line that lost the shelf (<= 200 chars, starts
  with the prefix the client, the filter and the publish cleanup look for)."""
  taker = c.get('decision_taker')
  need = float(c.get('qty_open') or 0)
  who = f"order {taker} is planned for it" if taker else 'it is spoken for on this plan'
  return (f"{_SHELF_DECISION_PREFIX} needs {need:g}, the shelf covers one order - {who}. "
          f"Decide with Sales before publish: Remove this line (ship {taker or 'the other'} complete) "
          f"or plan a quantity on each (star)")[:200]


def _drop_undecided_shelf_lines(cursor, plan_date, actor):
  """v1.4: at publish an undecided SHELF DECISION line leaves the plan -
  the priority pick stands. Returns the labels removed."""
  cursor.execute("""
      SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, priority_note
      FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND line_status IN ('Planned', 'Proposed', 'Confirmed')
        AND priority_note LIKE %s
  """, (plan_date, _SHELF_DECISION_PREFIX + '%'))
  gone = []
  for r in cursor.fetchall():
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Removed', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (actor, plan_date, r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']))
    _log_event(cursor, plan_date, r['nx_order_numb'], r['nx_sku'], 'Removed',
               old_value='shelf decision', new_value='undecided at publish',
               actor=actor, note='the shelf stays with the order planned first: ' + (r['priority_note'] or '')[:400],
               rel=r['nx_rel_numb'], item=r['nx_item_no'])
    gone.append(_line_label(r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no'], r['nx_sku']))
  return gone


def _system_prefix(note):
  """The system clause at the front of a board note ('SPLIT n of N',
  'SHELF DECISION: ...') that a hand edit must not lose; '' when none."""
  n = (note or '')
  if n.startswith('SPLIT '):
    # v1.5: the multi-day day clause rides inside the prefix ('SPLIT 100 of
    # 315 · day 2 of about 4') - the next morning's prior-share query reads it
    m = re.match(r'SPLIT \S+ of \S+( · day \d+ of about \d+)?', n)
    if not m:
      return ''
    if m.group(1):
      return _md_system_text(n)       # v1.6.1: assemblers, partials rule, pause ride with it
    return m.group(0)
  if n.startswith(_MD_DONE_PREFIX):   # v1.6.1: a built line's note survives a hand edit too
    return _md_system_text(n)
  if n.startswith(_SHELF_DECISION_PREFIX):
    return n.split(' · ')[0]
  return ''


def _md_system_text(note):
  """v1.6.1: the machine part of a multi-day note - everything before the
  engine's tier reason (' · Must:' / ' · Next:' / ' · Stretch:'), which is
  the part PC's own words replace."""
  n = note or ''
  cuts = [i for i in (n.find(' · Must:'), n.find(' · Next:'), n.find(' · Stretch:')) if i > 0]
  return n[:min(cuts)] if cuts else n


# --- v1.5 multi-day orders (daily-plan-multi-day-work-orders.md s16-18) ----
_MD_CLAUSE_RE = re.compile(r' · day (\d+) of about (\d+)')
_MD_PRIOR_LIKE = 'SPLIT % of % · day %'      # the board note of a day-sized share
_MD_DONE_PREFIX = 'MULTI-DAY: '              # a sibling whose shares already cover it


def _md_clause(note):
  """' · day d of about k' from a board note, or ''."""
  m = _MD_CLAUSE_RE.search(_system_prefix(note))
  return m.group(0) if m else ''


def _mdkey(m):
  return (m['nx_order_numb'], m['nx_rel_numb'], str(m['nx_item_no']))


def _md_lot(pieces):
  """The lot a day-sized share rounds down to (paper s5: 10 / 25 / 50)."""
  p = float(pieces or 0)
  return 50 if p >= 500 else 25 if p >= 100 else 10 if p >= 20 else 1


def _md_round(pieces):
  lot = _md_lot(pieces)
  return float(int(float(pieces) / lot) * lot)


def _md_prior_shares(cursor, plan_date):
  """Shares already planned for each line on EARLIER published plans:
  {(order, rel, item): {'qty': sum, 'days': {plan_date, ...}}}. Until the
  tablet count arrives (step 2) the engine assumes those shares were built
  (Dave 2026-10-25). 45-day window; a line that shipped is no candidate."""
  cursor.execute("""
      SELECT pl.nx_order_numb, pl.nx_rel_numb, pl.nx_item_no, pl.plan_date, pl.qty_planned
      FROM dbo.pc_ship_plan_line pl
      JOIN dbo.pc_ship_plan h ON h.plan_date = pl.plan_date
      WHERE h.plan_status = 'Published'
        AND pl.plan_date < %s AND pl.plan_date >= DATEADD(day, -45, %s)
        AND pl.line_status IN ('Planned', 'Proposed', 'Confirmed')
        AND pl.priority_note LIKE %s
  """, (plan_date, plan_date, _MD_PRIOR_LIKE))
  out = {}
  for r in cursor.fetchall():
    k = (r['nx_order_numb'], r['nx_rel_numb'], str(r['nx_item_no']))
    e = out.setdefault(k, {'qty': 0.0, 'days': set()})
    e['qty'] += float(r['qty_planned'] or 0)
    e['days'].add(r['plan_date'])
  return out


def _md_assemblers(cursor, orders):
  """{order: 'ASM9+ASM3'} - assemblers the PC app / Supervisor board has on
  the order's open work orders. Read as a fact (one owner per table, 074);
  shown on the note, not used in the share in this iteration."""
  orders = [o for o in set(orders or []) if o]
  if not orders:
    return {}
  cursor.execute("""
      SELECT sales_order_number AS o, assigned_assembler AS a
      FROM dbo.work_orders
      WHERE sales_order_number IN %s AND completed_at IS NULL
        AND NULLIF(LTRIM(RTRIM(assigned_assembler)), '') IS NOT NULL
      GROUP BY sales_order_number, assigned_assembler
  """, (tuple(orders),))
  out = {}
  for r in cursor.fetchall():
    out.setdefault(r['o'], []).append(str(r['a']).strip())
  return {o: '+'.join(sorted(v)) for o, v in out.items()}


def _md_tablet_facts(cursor, orders, days_back=30):
  """v1.5.1: the Assembly tablet per e21 line of the given orders, from the
  floor taps of the last days_back days (Assign is the board, not the floor;
  v1.6.2: assembly operators only - the Screen kiosk shares the table):
  {(order, rel, item): {'state': running | paused | done | None,
                        'count_total': pieces from Count taps (note 'pieces=N',
                        migration 118), 'done_at', 'first_start', 'last_tap'}}."""
  orders = [o for o in set(orders or []) if o]
  if not orders:
    return {}
  cursor.execute("""
      SELECT wo.sales_order_number AS o, wo.sales_order_rel AS rel,
             CAST(wo.sales_order_line AS VARCHAR(10)) AS item,
             e.event_type, e.event_at, e.note
      FROM dbo.work_order_events e
      JOIN dbo.work_orders wo ON wo.work_order_id = e.work_order_id
      WHERE wo.sales_order_number IN %s AND wo.status <> 'Cancelled'
        AND e.event_type IN ('Start', 'Restart', 'Stop', 'Complete', 'Reopen', 'Count')
        AND e.event_at >= DATEADD(day, -%s, CAST(GETDATE() AS DATE))
        -- v1.6.2: work_order_events is shared with the Screen / Element kiosk (OP codes,
        -- dbo.screen_operators, tapping since June). Only an ASSEMBLY operator's tap is an
        -- assembly fact: a screen Complete is the element, not the valve (597535, 10-26).
        AND (e.operator IN (SELECT operator_code FROM dbo.assembly_operators WHERE department_id = 1)
             OR e.note LIKE 'dept=Assembly%%')
      ORDER BY e.event_at, e.event_id
  """, (tuple(orders), int(days_back)))
  out = {}
  for e in cursor.fetchall():
    k = (e['o'], e['rel'] or '', str(e['item'] or ''))
    t = out.setdefault(k, {'state': None, 'count_total': 0, 'done_at': None,
                           'first_start': None, 'last_tap': None})
    at = e['event_at']
    d = at.date() if isinstance(at, datetime) else at
    t['last_tap'] = d
    typ = e['event_type']
    if typ in ('Start', 'Restart', 'Reopen'):
      t['state'] = 'running'
      t['first_start'] = t['first_start'] or d
    elif typ == 'Stop':
      t['state'] = 'paused'
    elif typ == 'Complete':
      t['state'] = 'done'
      t['done_at'] = at
    elif typ == 'Count':
      mm = re.search(r'pieces\s*=\s*(\d+)', e['note'] or '', re.I) or re.match(r'\s*(\d+)\s*$', e['note'] or '')
      if mm:
        t['count_total'] += int(mm.group(1))
        if t['state'] is None:
          t['state'] = 'running'
  return out


def _md_note(c, ship_complete):
  """Board note for a day-sized share. MUST start 'SPLIT n of N · day d of
  about k' (the prior-share query, _system_prefix, set_qty and recalc read
  that clause). <= 200 chars."""
  who = c.get('md_assemblers') or 'no assembler assigned yet'
  if c.get('md_planned_asm'):                 # v1.7: PC's headcount sets the share
    who = f"{float(c['md_planned_asm']):g} planned ({c.get('md_assemblers') or 'none assigned'})"
  mode = c.get('md_mode') or ('COMPLETE' if ship_complete else 'DECIDE')   # v1.6: PC's choice carries over
  tail = _MD_PARTIALS.get(mode, _MD_PARTIALS['DECIDE'])
  if c.get('md_check'):
    tail = 'earlier shares cover the order, not shipped - check the floor'
  stock = (f" · stock: the rest is planned on order {c.get('stock_taker') or '?'}"
           if c.get('stock_qty') else '')
  return (f"SPLIT {float(c['md_qty']):g} of {float(c['md_full']):g}"
          f" · day {int(c['md_day'])} of about {int(c['md_about'])} · {who} · {tail}{stock}")[:200]


_MD_PARTIALS = {'DECIDE': 'ship partial: PC decides',
                'OK': 'partials OK: ship daily',
                'COMPLETE': 'ships when complete'}
_MD_PAUSED_RE = re.compile(r' · PAUSED( to (\d{4}-\d{2}-\d{2}))?(: ([^·]*))?')


def _md_parse_note(note):
  """v1.6: what a board note says about a multi-day line, for the row and
  the star: {'share', 'full', 'day', 'about', 'built', 'partials' (DECIDE /
  OK / COMPLETE), 'paused_to', 'pause_reason'} or None when the line is not
  multi-day."""
  n = note or ''
  out = None
  m = re.match(r'SPLIT (\S+) of (\S+) · day (\d+) of about (\d+)', n)
  if m:
    out = {'share': float(m.group(1)), 'full': float(m.group(2)),
           'day': int(m.group(3)), 'about': int(m.group(4)), 'built': False}
  elif n.startswith(_MD_DONE_PREFIX):
    out = {'share': None, 'full': None, 'day': None, 'about': None, 'built': True}
  if out is None:
    return None
  out['partials'] = next((k for k, v in _MD_PARTIALS.items() if (' · ' + v) in n), 'DECIDE')
  pm = _MD_PAUSED_RE.search(n)
  out['paused_to'] = pm.group(2) if pm else None
  out['pause_reason'] = (pm.group(4) or '').strip() if pm else None
  return out


def _md_order_modes(cursor, plan_date):
  """v1.6: the partials mode PC last set per order (OK / COMPLETE), read from
  the newest multi-day note on any plan day in the last 45 days - including
  plan_date itself (the star may have been used before a re-run)."""
  cursor.execute("""
      SELECT nx_order_numb, priority_note FROM dbo.pc_ship_plan_line
      WHERE plan_date <= %s AND plan_date >= DATEADD(day, -45, %s)
        AND priority_note LIKE %s
      ORDER BY plan_date, updated_at
  """, (plan_date, plan_date, _MD_PRIOR_LIKE))
  out = {}
  for r in cursor.fetchall():
    md = _md_parse_note(r['priority_note'])
    if md and md['partials'] != 'DECIDE':
      out[r['nx_order_numb']] = md['partials']
  return out


def _md_assembler_day(cursor, cap):
  """v1.7: work-inches one assembler builds in a day. ASSEMBLER_DAY_IN pins
  it; blank = capacity / effective crew this morning (~165 in)."""
  p = _get_params(cursor)
  try:
    v = float(str(p.get('ASSEMBLER_DAY_IN', '') or '').strip() or 0)
  except (TypeError, ValueError):
    v = 0.0
  if v > 0:
    return v
  crew = float(cap.get('effective_crew') or 0)
  capi = float(cap.get('capacity_inches_effective') or 0)
  return (capi / crew) if crew > 0 else 0.0


def _md_plan_rows(cursor, plan_date, orders):
  """v1.7: PC's plan for the day from pc_multiday_plan (121):
  {order: {'assemblers': n or None, 'done': bool, 'note': str,
           'lines': {(rel, item): {'share': pcs or None, 'done': bool}}}}"""
  orders = [o for o in set(orders or []) if o]
  if not orders:
    return {}
  cursor.execute("""
      SELECT nx_order_numb, nx_rel_numb, nx_item_no, assemblers_planned, share_pcs, done_by_pc, note
      FROM dbo.pc_multiday_plan
      WHERE plan_date = %s AND nx_order_numb IN %s
  """, (plan_date, tuple(orders)))
  out = {}
  for r in cursor.fetchall():
    e = out.setdefault(r['nx_order_numb'], {'assemblers': None, 'done': False, 'note': None, 'lines': {}})
    if r['nx_rel_numb'] is None:
      e['assemblers'] = float(r['assemblers_planned']) if r['assemblers_planned'] is not None else None
      e['done'] = bool(r['done_by_pc'])
      e['note'] = r['note']
    else:
      e['lines'][(r['nx_rel_numb'], str(r['nx_item_no']))] = {
        'share': float(r['share_pcs']) if r['share_pcs'] is not None else None,
        'done': bool(r['done_by_pc'])}
  # done-by-PC on earlier days counts too (the order stays built)
  cursor.execute("""
      SELECT DISTINCT nx_order_numb, nx_rel_numb, nx_item_no FROM dbo.pc_multiday_plan
      WHERE plan_date < %s AND plan_date >= DATEADD(day, -45, %s) AND done_by_pc = 1
        AND nx_order_numb IN %s
  """, (plan_date, plan_date, tuple(orders)))
  for r in cursor.fetchall():
    e = out.setdefault(r['nx_order_numb'], {'assemblers': None, 'done': False, 'note': None, 'lines': {}})
    if r['nx_rel_numb'] is None:
      e['done'] = True
    else:
      e['lines'].setdefault((r['nx_rel_numb'], str(r['nx_item_no'])), {'share': None, 'done': False})['done'] = True
  return out


def _md_done_note(c):
  t = c.get('md_tablet') or {}
  if c.get('md_done_pc'):
    return (f"{_MD_DONE_PREFIX}built - PC marked done (multi-day page)"
            " · rides at 0 in until the order ships")[:200]
  if c.get('md_done_fact'):
    when = t.get('done_at')
    return (f"{_MD_DONE_PREFIX}built - tablet Complete"
            + (f" {when:%m/%d %H:%M}" if when else '')
            + " · rides at 0 in until the order ships")[:200]
  days = sorted(c.get('md_days') or [])
  span = (f"{str(days[0])[:10]}" + (f" to {str(days[-1])[:10]}" if len(days) > 1 else '')) if days else 'earlier'
  return (f"{_MD_DONE_PREFIX}{float(c['md_full']):g} planned over {len(days) or 1} day(s) ({span}), "
          f"est. built · rides at 0 in until the order ships")[:200]


@anvil.server.callable
def ship_plan_set_qty(plan_date, order, rel, item, qty, why=None):
  """Decision 5 (2026-10-10, built 10-12): plan ANY line at a quantity.
  qty below the ordered quantity = a deliberate partial: load scales per
  piece, the board note starts 'SPLIT n of N - PARTIAL' (drift rule,
  Refresh, the Partial filter); qty at or above it = the whole line again
  (note cleared of the SPLIT / SHELF DECISION clause). Logged as Recalc
  (the only re-total event type) with a PARTIAL note."""
  plan_date = _parse_date(plan_date)
  try:
    qty = float(qty)
  except Exception:
    return {'status': 'error', 'message': 'Quantity must be a number.'}
  if qty <= 0:
    return {'status': 'error', 'message': 'Quantity must be above zero.'}
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT qty_planned, load_inches, priority_note, line_status FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND line_status IN %s
    """, (plan_date, order, rel, item, _ACTIVE_STATUSES))
    old = cursor.fetchone()
    if not old:
      return {'status': 'error', 'message': 'Line not found or not active.'}
    rows = _fetch_candidate_rows(cursor, order, rel, item)
    if not rows:
      return {'status': 'error', 'message': 'No open quantity in the feed - this line likely shipped.'}
    agg = _aggregate(rows)
    full = float(agg['qty'] or 0)
    old_note = old.get('priority_note') or ''
    tail = old_note[len(_system_prefix(old_note)):].lstrip(' -·:') if _system_prefix(old_note) else old_note
    tail = re.sub(r'^PARTIAL(\s*\(gate [^)]*\))?:?\s*', '', tail).strip()
    if re.match(r'(Must|Next|Stretch): ', tail):
      tail = ''                            # v1.6.2: the engine's reason sentence is not PC's why
    why = (why or '').strip()
    if qty >= full:                      # the whole line again
      qty = full
      note = (tail or None)
    else:
      agg = _scale_agg(agg, qty)
      # v1.5: a multi-day share keeps its day clause so tomorrow's run still
      # counts today's (new) quantity as a prior share
      pfx0 = _system_prefix(old_note)
      m0 = re.match(r'SPLIT \S+ of \S+ · day \d+ of about \d+', pfx0)
      md_tail = pfx0[m0.end():] if m0 else ''       # v1.6.1: ' · ASM9 · partials OK: ship daily' survives
      note = (_split_note(qty, full) + _md_clause(old_note) + md_tail + ' - PARTIAL'
              + ': ' + (why or tail or 'quantity set by PC'))
      note = note[:200]
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET qty_planned = %s, load_inches = %s, load_minutes = %s, surcharge_minutes = %s,
            priority_note = %s, updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (agg['qty'], agg['inches'], agg['minutes'], agg['surcharge'], note, actor,
          plan_date, order, rel, item))
    old_txt = f"{float(old['qty_planned']):g} pcs / {float(old['load_inches']):.1f} in"
    new_txt = f"{agg['qty']:g} pcs / {agg['inches']:.1f} in"
    _log_event(cursor, plan_date, order, agg['sku'], 'Recalc',
               old_value=old_txt, new_value=new_txt, actor=actor,
               note=(('PARTIAL: ' + (why or 'quantity set by PC')) if qty < full
                     else 'whole line again' + (': ' + why if why else '')), rel=rel, item=item)
    _bump_revision(cursor, plan_date, actor)
    planned = _planned_inches(cursor, plan_date)
    cap = _capacity_for(cursor, plan_date)['capacity_inches_effective']
    conn.commit()
    return {'status': 'success', 'old': old_txt, 'new': new_txt, 'qty': agg['qty'],
            'full': full, 'planned_inches': round(planned, 1),
            'capacity_inches': round(cap, 1), 'over_capacity': planned > cap}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Quantity failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_multiday_set(plan_date, order, partials=None, pause_to=None, pause_reason=None):
  """v1.6 (step 3, decisions 5 and 6): PC's two multi-day decisions for an
  ORDER, from the star of any of its lines.
  partials: 'OK' (ship each day's pieces) / 'COMPLETE' (hold until the
    order is built) / 'DECIDE' (back to undecided). Rewrites the mode clause
    on every multi-day note of the order on this plan; the engine carries
    the choice onto tomorrow's share (_md_order_modes).
  pause_to + pause_reason: the order leaves the plan until that date. Its
    active lines become Snoozed (snooze_to_date = pause_to) WITHOUT a
    forward copy - the generic snooze would plan the WHOLE line on the
    return day; the morning run re-proposes the right share instead,
    first in line (started order). The note keeps its SPLIT clause and
    gains ' · PAUSED to <date>: <reason>'. Logged as Snoozed / ReasonSet.
    The paused lines stay Snoozed on this plan day (Resume does not revive
    them today - the plan line's PK is taken; tomorrow's run proposes anew)."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  partials = (partials or '').strip().upper() or None
  if partials and partials not in _MD_PARTIALS:
    return {'status': 'error', 'message': 'partials must be OK, COMPLETE or DECIDE.'}
  if pause_to:
    pause_to = _parse_date(pause_to)
    if pause_to <= plan_date:
      return {'status': 'error', 'message': 'The pause must end after the plan day.'}
    if not (pause_reason or '').strip():
      return {'status': 'error', 'message': 'A pause needs a reason (decision 6).'}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_rel_numb, nx_item_no, nx_sku, priority_note, line_status
        FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND line_status IN %s
          AND (priority_note LIKE %s OR priority_note LIKE %s)
    """, (plan_date, order, ('Planned', 'Proposed', 'Confirmed'), _MD_PRIOR_LIKE, _MD_DONE_PREFIX + '%'))
    lines = cursor.fetchall()
    if not lines:
      return {'status': 'error', 'message': f'{order} has no multi-day line on {plan_date}.'}
    changed = []
    for l in lines:
      note = l['priority_note'] or ''
      new = note
      if partials:
        for v in _MD_PARTIALS.values():
          new = new.replace(' · ' + v, '')
        new = new.replace(' · earlier shares cover the order, not shipped - check the floor', '')
        head, sep, rest = new.partition(' · Must:') if ' · Must:' in new else new.partition(' · Next:')
        new = (head + ' · ' + _MD_PARTIALS[partials] + sep + rest) if sep else (new + ' · ' + _MD_PARTIALS[partials])
      if pause_to:
        new = _MD_PAUSED_RE.sub('', new)
        clause = f" · PAUSED to {pause_to.isoformat()}: {pause_reason.strip()}"
        new = (new[:200 - len(clause)] + clause) if len(new) + len(clause) > 200 else new + clause
      new = new[:200]
      if pause_to:
        cursor.execute("""
            UPDATE dbo.pc_ship_plan_line
            SET line_status = 'Snoozed', snooze_to_date = %s, priority_note = %s,
                updated_by = %s, updated_at = GETDATE()
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
        """, (pause_to, new, actor, plan_date, order, l['nx_rel_numb'], l['nx_item_no']))
        _log_event(cursor, plan_date, order, l['nx_sku'], 'Snoozed',
                   old_value=l['line_status'], new_value=pause_to.isoformat(), actor=actor,
                   note=('PAUSED (multi-day): ' + pause_reason.strip())[:400],
                   rel=l['nx_rel_numb'], item=l['nx_item_no'])
      elif new != note:
        cursor.execute("""
            UPDATE dbo.pc_ship_plan_line SET priority_note = %s, updated_by = %s, updated_at = GETDATE()
            WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
        """, (new, actor, plan_date, order, l['nx_rel_numb'], l['nx_item_no']))
        _log_event(cursor, plan_date, order, l['nx_sku'], 'ReasonSet',
                   old_value=note[:200], new_value=new[:200], actor=actor,
                   note=f'MULTI-DAY partials: {partials}', rel=l['nx_rel_numb'], item=l['nx_item_no'])
      changed.append(_line_label(order, l['nx_rel_numb'], l['nx_item_no'], l['nx_sku']))
    _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success', 'order': order, 'n_lines': len(changed), 'lines': changed,
            'partials': partials, 'paused_to': pause_to.isoformat() if pause_to else None}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Multi-day update failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_multiday_resume(order, note=None):
  """v1.6: end a pause early. The paused lines (Snoozed with a PAUSED clause,
  any plan day) get snooze_to_date = yesterday so they no longer block the
  order; the next morning run proposes it first (not today: the day's
  lines keep their Snoozed row)."""
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT plan_date, nx_rel_numb, nx_item_no, nx_sku, priority_note, snooze_to_date
        FROM dbo.pc_ship_plan_line
        WHERE nx_order_numb = %s AND line_status = 'Snoozed' AND priority_note LIKE %s
          AND ISNULL(snooze_to_date, plan_date) >= CAST(GETDATE() AS DATE)
    """, (order, '% · PAUSED%'))
    rows = cursor.fetchall()
    if not rows:
      return {'status': 'error', 'message': f'{order} is not paused.'}
    for l in rows:
      new = _MD_PAUSED_RE.sub(f" · RESUMED {date.today().isoformat()}", l['priority_note'] or '')[:200]
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET snooze_to_date = DATEADD(day, -1, CAST(GETDATE() AS DATE)), priority_note = %s,
              updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
      """, (new, actor, l['plan_date'], order, l['nx_rel_numb'], l['nx_item_no']))
      _log_event(cursor, l['plan_date'], order, l['nx_sku'], 'ReasonSet',
                 old_value=f"paused to {l['snooze_to_date']}", new_value='resumed', actor=actor,
                 note=('RESUMED (multi-day)' + (': ' + note if note else ''))[:400],
                 rel=l['nx_rel_numb'], item=l['nx_item_no'])
    conn.commit()
    return {'status': 'success', 'order': order, 'n_lines': len(rows),
            'message': f'{order} resumed - it is proposed first on the next morning run.'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Resume failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_multiday_plan_set(order, plan_date, assemblers=None, rel=None, item=None,
                                share_pcs=None, done=None, note=None):
  """v1.7 (multi-day page): PC's plan for an order on a day (pc_multiday_plan,
  121). rel/item None = the whole order (assemblers, done); with rel/item =
  one line (share override, done). A None argument leaves that field alone;
  share_pcs or assemblers of 0 / '' clears it. Logged as ParamChange."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  def _num(v):
    if v is None:
      return None
    sv = str(v).strip()
    if sv == '':
      return 0.0
    return float(sv)
  try:
    asm = _num(assemblers)
    share = _num(share_pcs)
  except ValueError:
    return {'status': 'error', 'message': 'Assemblers and share must be numbers.'}
  if (rel is None) != (item is None):
    return {'status': 'error', 'message': 'rel and item go together.'}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT multiday_id, assemblers_planned, share_pcs, done_by_pc, note FROM dbo.pc_multiday_plan
        WHERE nx_order_numb = %s AND plan_date = %s
          AND ((nx_rel_numb IS NULL AND %s IS NULL) OR (nx_rel_numb = %s AND nx_item_no = %s))
    """, (order, plan_date, rel, rel, item))
    row = cursor.fetchone()
    new_asm = (None if (asm is not None and asm <= 0) else asm) if asm is not None else (row['assemblers_planned'] if row else None)
    new_share = (None if (share is not None and share <= 0) else share) if share is not None else (row['share_pcs'] if row else None)
    new_done = (1 if done else 0) if done is not None else (int(row['done_by_pc']) if row else 0)
    new_note = (note[:200] if note is not None else (row['note'] if row else None))
    if rel is not None:
      new_asm = None                       # headcount is an order-level fact
    if row:
      cursor.execute("""
          UPDATE dbo.pc_multiday_plan
          SET assemblers_planned = %s, share_pcs = %s, done_by_pc = %s, note = %s,
              set_by = %s, set_at = GETDATE()
          WHERE multiday_id = %s
      """, (new_asm, new_share, new_done, new_note, actor, row['multiday_id']))
    else:
      cursor.execute("""
          INSERT INTO dbo.pc_multiday_plan
              (nx_order_numb, plan_date, nx_rel_numb, nx_item_no, assemblers_planned, share_pcs, done_by_pc, note, set_by)
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
      """, (order, plan_date, rel, item, new_asm, new_share, new_done, new_note, actor))
    what = (f"assemblers {new_asm if new_asm is not None else 'pool'}" if rel is None
            else f"share {new_share if new_share is not None else 'computed'}") + (', done' if new_done else '')
    _log_event(cursor, plan_date, order, None, 'ParamChange',
               old_value=(f"asm {row['assemblers_planned']} share {row['share_pcs']} done {row['done_by_pc']}" if row else None),
               new_value=what[:200], actor=actor,
               note=('multi-day page' + (': ' + new_note if new_note else ''))[:400], rel=rel, item=item)
    conn.commit()
    return {'status': 'success', 'order': order, 'plan_date': plan_date.isoformat(),
            'assemblers': new_asm, 'share_pcs': new_share, 'done': bool(new_done)}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Multi-day plan failed: {e}'}
  finally:
    conn.close()


def _md_day_list(plan_rows, projection, today):
  """v1.7.1: the page's day columns, oldest first: every plan day the order
  has a live or paused line on, plus the projection days. Labels are made
  here (the client has no strftime worth trusting)."""
  tkey = today.isoformat()
  seen, days = set(), []
  for r in plan_rows:
    d = str(r['plan_date'])[:10]
    if r['line_status'] in ('Planned', 'Proposed', 'Confirmed', 'Snoozed') and d not in seen:
      seen.add(d); days.append({'date': d, 'kind': 'board' if d == tkey else ('past' if d < tkey else 'planned')})
  for pr in projection:
    if pr['plan_date'] not in seen:
      seen.add(pr['plan_date']); days.append({'date': pr['plan_date'], 'kind': 'est'})
  days.sort(key=lambda x: x['date'])
  for x in days:
    dd = date.fromisoformat(x['date'])
    lab = dd.strftime('%a %m/%d')
    x['label'] = lab + {'board': ' (today)', 'est': ' est.', 'planned': ' (on board)'}.get(x['kind'], '')
    x['on_board'] = x['kind'] in ('board', 'planned')
  return days


def _md_grid_markdown(order, lines, rem0, plan_rows, pc_rows, taps, wos, projection, today, asm_day, pool):
  """v1.7.1: the Multi-day Order page grid as a markdown table (the page's
  RichText renders it; the gates page uses the same device). Lines down,
  days across: past plan days (planned / counted), today (on the board),
  the projection (est.), then what is left."""
  def dkey(v):
    return str(v)[:10]
  day_list = _md_day_list(plan_rows, projection, today)
  days = [x['date'] for x in day_list]
  labels = {x['date']: x['label'] for x in day_list}
  tkey = today.isoformat()
  def dlabel(d):
    return labels[d]
  pc_asm = {dkey(r['plan_date']): r['assemblers_planned'] for r in pc_rows if r['nx_rel_numb'] is None}
  proj_asm = {pr['plan_date']: pr['assemblers_planned'] for pr in projection}
  assigned = sorted({w['assigned_assembler'] for w in wos if w.get('assigned_assembler')})
  # per line per day: planned pieces (plan rows) / counted (taps) / projected
  planned = {}
  for r in plan_rows:
    if r['line_status'] in ('Planned', 'Proposed', 'Confirmed', 'Snoozed'):
      planned[(r['nx_rel_numb'], str(r['nx_item_no']), dkey(r['plan_date']))] = r
  counted, tapped = {}, {}
  for t in taps:
    k = (t['rel'], str(t['item']), dkey(t['d']))
    tapped.setdefault(k, []).append(t['event_type'])
    if t['event_type'] == 'Count':
      m = re.search(r'pieces\s*=\s*(\d+)', t.get('note') or '', re.I)
      if m:
        counted[k] = counted.get(k, 0) + int(m.group(1))
  proj = {}
  for pr in projection:
    for l in pr['lines']:
      proj[(l['rel'], str(l['item']), pr['plan_date'])] = l
  head = '| | ' + ' | '.join(dlabel(d) for d in days) + ' | left |'
  sep = '|---|' + '---|' * len(days) + '---|'
  rows = [head, sep]
  rows.append('| assemblers planned | ' + ' | '.join(
    (f"{float(pc_asm[d]):g}" if pc_asm.get(d) is not None else (f"{float(proj_asm[d]):g}" if proj_asm.get(d) else 'pool'))
    for d in days) + ' | |')
  rows.append('| assemblers assigned | ' + ' | '.join(('+'.join(assigned) or '-') for d in days) + ' | |')
  for l in lines:
    k2 = (l['nx_rel_numb'], str(l['nx_item_no']))
    r0 = rem0[k2]
    cells = []
    for d in days:
      p = planned.get(k2 + (d,))
      c = counted.get(k2 + (d,))
      pj = proj.get(k2 + (d,))
      if p is not None:
        md = _md_parse_note(p.get('priority_note'))
        if md and md.get('built'):
          txt = 'built'
        else:
          txt = f"{float(p['qty_planned']):g} planned" + (' (on board)' if d == tkey else '')
        if p['line_status'] == 'Snoozed':
          txt = 'paused'
        if c is not None:
          txt += f" / {c} counted"
      elif pj is not None:
        txt = f"~{float(pj['pieces']):g}" + (' (PC)' if pj.get('override') else '')
      else:
        txt = '-'
      cells.append(txt)
    left = 'built' if r0['done'] else f"{r0['remaining']:g}"
    rows.append(f"| -{l['nx_item_no']} {l['nx_sku']} ({float(l['qty'] or 0):g} pc) | " + ' | '.join(cells) + f" | {left} |")
  # load row
  loads = []
  for d in days:
    v = sum(float(planned[k]['load_inches'] or 0) for k in planned if k[2] == d)
    if v == 0 and d > tkey:
      v = next((pr['inches'] for pr in projection if pr['plan_date'] == d), 0.0)
    loads.append(f"{v:,.0f}" + (' est.' if d > tkey else ''))
  left_in = sum(v['remaining'] * v['per_piece'] for v in rem0.values())
  rows.append('| load (in) | ' + ' | '.join(loads) + f" | {left_in:,.0f} |")
  tap_cells = []
  for d in days:
    ev = [e for k, v in tapped.items() if k[2] == d for e in v]
    tap_cells.append(', '.join(f"{ev.count(x)} {x}" for x in ('Start', 'Count', 'Stop', 'Restart', 'Complete') if ev.count(x)) or ('no tap' if d <= tkey else '-'))
  rows.append('| tablet (assembly) | ' + ' | '.join(tap_cells) + ' | |')
  return '\n'.join(rows)


@anvil.server.callable
def ship_plan_multiday_get(order, days_ahead=5, days_back=10):
  """v1.7: everything the Multi-day Order page shows for one order: the
  lines, the past plan days (share planned, assembly taps / counts), today,
  PC's plan rows, assigned assemblers, and a PROJECTION of the next business
  days drawn with the engine's arithmetic (assemblers planned x one
  assembler-day, pool-capped; lot rounding; one cut line per day). Nothing
  here is a commitment - the 06:30 run decides."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    today = date.today()
    ap = _agent_params(cursor)
    nb = _next_business_day()
    cursor.execute("""
        SELECT r.nx_order_numb, r.nx_rel_numb, r.nx_item_no, r.nx_sku, r.nx_sku_desc,
               ISNULL(r.nx_qty_ordered, r.nx_qty_balance_due) AS qty, r.work_inches, r.minutes_charged,
               CAST(COALESCE(r.nx_dship_by_date, r.nx_hship_by_date) AS DATE) AS ship_by, r.nx_shipto_code,
               c.nx_cust_name, c.nx_cust_city, c.nx_cust_state
        FROM dbo.vw_pc_ship_candidate_rows r
        OUTER APPLY (SELECT TOP 1 s.nx_cust_name, s.nx_cust_city, s.nx_cust_state FROM dbo.sales_orders_due s
                     WHERE s.nx_order_numb = r.nx_order_numb) c
        WHERE r.nx_order_numb = %s
        ORDER BY r.nx_rel_numb, TRY_CAST(r.nx_item_no AS INT), r.nx_item_no
    """, (order,))
    lines = [serialize_row(r) for r in cursor.fetchall()]
    if not lines:
      return {'status': 'error', 'message': f'{order}: no open lines in the feed.'}
    cursor.execute("SELECT TOP 1 RTRIM(nx_order_type) AS t FROM dbo.sales_orders_due WHERE nx_order_numb = %s", (order,))
    otype = (cursor.fetchone() or {}).get('t')
    # past + today plan rows for the order
    cursor.execute("""
        SELECT pl.plan_date, pl.nx_rel_numb, pl.nx_item_no, pl.qty_planned, pl.load_inches, pl.line_status,
               pl.priority_note, h.plan_status
        FROM dbo.pc_ship_plan_line pl JOIN dbo.pc_ship_plan h ON h.plan_date = pl.plan_date
        WHERE pl.nx_order_numb = %s AND pl.plan_date >= DATEADD(day, -%s, %s) AND pl.plan_date <= %s
        ORDER BY pl.plan_date
    """, (order, int(days_back), today, nb))
    plan_rows = [serialize_row(r) for r in cursor.fetchall()]
    # v1.7.1: the projection starts on the first day WITHOUT a plan (today's
    # board already says what today is); capacity for it is the roster
    # estimate, the same basis the 06:30 run plans with
    last_planned = max([date.fromisoformat(str(r['plan_date'])[:10]) for r in plan_rows
                        if r['line_status'] in ('Planned', 'Proposed', 'Confirmed')] or [today - timedelta(days=1)])
    start = _next_business_day(max(nb, last_planned + timedelta(days=1)))
    cap = _capacity_for(cursor, start, prefer_estimate=True)
    cap_in = float(cap.get('capacity_inches_effective') or 0)
    target = cap_in * ap['fill_pct']
    pool = target * ap['multiday_share_pct']
    asm_day = _md_assembler_day(cursor, cap)
    cursor.execute("""
        SELECT TOP 1 snooze_to_date, priority_note FROM dbo.pc_ship_plan_line
        WHERE nx_order_numb = %s AND line_status = 'Snoozed' AND priority_note LIKE %s
          AND ISNULL(snooze_to_date, plan_date) >= CAST(GETDATE() AS DATE)
        ORDER BY snooze_to_date DESC
    """, (order, '% · PAUSED%'))
    pz = cursor.fetchone()
    paused = ({'to': str(pz['snooze_to_date'])[:10],
               'reason': (_md_parse_note(pz['priority_note']) or {}).get('pause_reason')} if pz else None)
    # PC's plan rows (today .. ahead)
    cursor.execute("""
        SELECT plan_date, nx_rel_numb, nx_item_no, assemblers_planned, share_pcs, done_by_pc, note, set_by, set_at
        FROM dbo.pc_multiday_plan
        WHERE nx_order_numb = %s AND plan_date >= DATEADD(day, -%s, %s)
        ORDER BY plan_date
    """, (order, int(days_back), today))
    pc_rows = [serialize_row(r) for r in cursor.fetchall()]
    # assembly taps per line per day
    cursor.execute("""
        SELECT wo.sales_order_rel AS rel, CAST(wo.sales_order_line AS VARCHAR(10)) AS item,
               CAST(e.event_at AS DATE) AS d, e.event_type, e.operator, e.note
        FROM dbo.work_order_events e
        JOIN dbo.work_orders wo ON wo.work_order_id = e.work_order_id
        LEFT JOIN dbo.assembly_operators ao ON ao.operator_code = e.operator AND ao.department_id = 1
        WHERE wo.sales_order_number = %s AND wo.status <> 'Cancelled'
          AND e.event_type IN ('Start', 'Restart', 'Stop', 'Complete', 'Reopen', 'Count')
          AND (ao.operator_code IS NOT NULL OR e.note LIKE 'dept=Assembly%%')
          AND e.event_at >= DATEADD(day, -%s, %s)
        ORDER BY e.event_at
    """, (order, int(days_back), today))
    taps = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT sales_order_rel AS rel, CAST(sales_order_line AS VARCHAR(10)) AS item,
               assigned_assembler, status, work_order_number
        FROM dbo.work_orders WHERE sales_order_number = %s AND status <> 'Cancelled'
    """, (order,))
    wos = [serialize_row(r) for r in cursor.fetchall()]
    tab = _md_tablet_facts(cursor, [order])
    prior = _md_prior_shares(cursor, nb + timedelta(days=1))
    modes = _md_order_modes(cursor, nb)
    # --- projection ----------------------------------------------------
    pc_by_day = {}
    for r in pc_rows:
      d = str(r['plan_date'])[:10]
      e = pc_by_day.setdefault(d, {'assemblers': None, 'done': False, 'lines': {}})
      if r['nx_rel_numb'] is None:
        e['assemblers'] = r['assemblers_planned']; e['done'] = bool(r['done_by_pc'])
      else:
        e['lines'][(r['nx_rel_numb'], str(r['nx_item_no']))] = {'share': r['share_pcs'], 'done': bool(r['done_by_pc'])}
    rem = {}
    for l in lines:
      k = (l['nx_rel_numb'], str(l['nx_item_no']))
      need = float(l['qty'] or 0)
      t = tab.get((order, l['nx_rel_numb'], str(l['nx_item_no']))) or {}
      pr = prior.get((order, l['nx_rel_numb'], str(l['nx_item_no'])))
      built = float(t.get('count_total') or 0) or (float(pr['qty']) if pr else 0.0)
      done = (t.get('state') == 'done') or any(x.get('lines', {}).get(k, {}).get('done') or x.get('done') for x in pc_by_day.values())
      per = (float(l['work_inches'] or 0) / need) if need > 0 else 0.0
      rem[k] = {'remaining': 0.0 if done else max(0.0, need - built), 'per_piece': per, 'done': done,
                'built_basis': ('tablet count' if t.get('count_total') else ('tablet Complete' if t.get('state') == 'done' else ('PC' if done else ('earlier shares' if pr else 'nothing'))))}
    rem0 = {k: dict(v) for k, v in rem.items()}       # v1.7.1: the line list shows the state BEFORE projecting
    projection, d, n = [], start, 0
    while n < int(days_ahead) and any(v['remaining'] >= 1 for v in rem.values()):
      ds = d.isoformat()
      pc = pc_by_day.get(ds) or {}
      asm = pc.get('assemblers')
      budget = min(pool, float(asm) * asm_day) if (asm and asm_day > 0) else pool
      todays, take_in, cut = [], 0.0, False
      for k, v in sorted(rem.items(), key=lambda kv: kv[1]['remaining'] * kv[1]['per_piece']):
        if v['remaining'] < 1 or v['per_piece'] <= 0:
          continue
        ov = (pc.get('lines') or {}).get(k, {}).get('share')
        if ov is not None:
          pcs = min(v['remaining'], float(ov))
        elif cut:
          continue
        else:
          left = budget - take_in
          if v['remaining'] * v['per_piece'] <= left + 1e-9:
            pcs = v['remaining']
          else:
            cut = True
            pcs = _md_round(left / v['per_piece'])
            if v['remaining'] - pcs < _md_lot(v['remaining']) and v['remaining'] * v['per_piece'] <= left * 1.15:
              pcs = v['remaining']
        if pcs < 1:
          continue
        todays.append({'rel': k[0], 'item': k[1], 'pieces': pcs, 'inches': round(pcs * v['per_piece'], 1),
                       'override': ov is not None})
        take_in += pcs * v['per_piece']
        v['remaining'] -= pcs
      projection.append({'plan_date': ds, 'assemblers_planned': asm, 'budget_inches': round(budget, 1),
                         'inches': round(take_in, 1), 'lines': todays, 'estimate': True})
      n += 1
      d = _next_business_day(d + timedelta(days=1))
    left_in = sum(v['remaining'] * v['per_piece'] for v in rem.values())
    return {'status': 'success', 'order': order, 'order_type': otype,
            'customer': _customer_label(lines[0].get('nx_cust_name'), lines[0].get('nx_cust_city'),
                                        lines[0].get('nx_cust_state'), lines[0].get('nx_shipto_code')),
            'ship_by': lines[0].get('ship_by'), 'partials': modes.get(order) or 'DECIDE',
            'lines': [{'rel': l['nx_rel_numb'], 'item': l['nx_item_no'], 'sku': l['nx_sku'], 'desc': l.get('nx_sku_desc'),
                       'qty': float(l['qty'] or 0), 'inches': float(l['work_inches'] or 0),
                       'remaining': rem0[(l['nx_rel_numb'], str(l['nx_item_no']))]['remaining'],
                       'built_basis': rem0[(l['nx_rel_numb'], str(l['nx_item_no']))]['built_basis'],
                       'done': rem0[(l['nx_rel_numb'], str(l['nx_item_no']))]['done'],
                       'assigned': [w['assigned_assembler'] for w in wos if w['rel'] == l['nx_rel_numb'] and w['item'] == str(l['nx_item_no']) and w.get('assigned_assembler')]}
                      for l in lines],
            'plan_rows': plan_rows, 'pc_rows': pc_rows, 'taps': taps, 'work_orders': wos,
            'pool_inches': round(pool, 1), 'assembler_day_in': round(asm_day, 1),
            'crew': cap.get('effective_crew'), 'capacity_inches': round(cap_in, 1),
            'projection': projection, 'remaining_inches': round(left_in, 1),
            'days_left_est': len(projection) if left_in <= 0.5 else None,
            'paused': paused, 'today': today.isoformat(), 'board_date': nb.isoformat(),
            'projection_start': start.isoformat(),
            'days': _md_day_list(plan_rows, projection, today),
            'grid_md': _md_grid_markdown(order, lines, rem0, plan_rows, pc_rows, taps, wos, projection, today, asm_day, pool)}
  except Exception as e:
    return {'status': 'error', 'message': f'Multi-day page load failed: {e}'}
  finally:
    conn.close()


def _md_candidate_text(cursor, rows):
  """v1.6: {(order, rel, item): text} for candidate rows that belong to a
  multi-day order - started (shares on earlier published plans), paused by
  PC, or over the max-line test (will be proposed at a share)."""
  if not rows:
    return {}
  nb = _next_business_day()
  prior = _md_prior_shares(cursor, nb + timedelta(days=1))
  cursor.execute("""
      SELECT nx_order_numb, MAX(snooze_to_date) AS to_date, MAX(priority_note) AS note
      FROM dbo.pc_ship_plan_line
      WHERE line_status = 'Snoozed' AND priority_note LIKE %s
        AND ISNULL(snooze_to_date, plan_date) >= CAST(GETDATE() AS DATE)
      GROUP BY nx_order_numb
  """, ('% · PAUSED%',))
  paused = {r['nx_order_numb']: r for r in cursor.fetchall()}
  ap = _agent_params(cursor)
  cap = float(_capacity_for(cursor, nb, prefer_estimate=True).get('capacity_inches_effective') or 0)
  max_line = cap * ap['fill_pct'] * ap['max_line_pct']
  by_order, started_orders = {}, {}
  for r in rows:
    k = (r['nx_order_numb'], r['nx_rel_numb'], str(r['nx_item_no']))
    gk = (r['nx_order_numb'], str(r.get('ship_by_date') or '')[:10])
    by_order[gk] = by_order.get(gk, 0.0) + float(r.get('load_inches') or 0)
    pr = prior.get(k)
    if pr:
      e = started_orders.setdefault(r['nx_order_numb'], {'qty': 0.0, 'full': 0.0, 'first': None})
      e['qty'] += pr['qty']; e['full'] += float(r.get('qty_open') or 0)
      d0 = min(pr['days'])
      e['first'] = d0 if e['first'] is None or d0 < e['first'] else e['first']
  out = {}
  for r in rows:
    k = (r['nx_order_numb'], r['nx_rel_numb'], str(r['nx_item_no']))
    o = r['nx_order_numb']
    gk = (o, str(r.get('ship_by_date') or '')[:10])
    if o in paused:
      p = paused[o]
      md = _md_parse_note(p['note']) or {}
      out[k] = (f"multi-day · PAUSED to {str(p['to_date'])[:10]}"
                + (f": {md.get('pause_reason')}" if md.get('pause_reason') else ''))
    elif o in started_orders:
      e = started_orders[o]
      out[k] = (f"multi-day · started {e['first']:%m/%d} · {e['qty']:g} of {e['full']:g} planned so far"
                + (' · comes back first' ))
    elif ap['multiday_share_pct'] > 0 and max_line > 0 and by_order.get(gk, 0.0) > max_line + 1e-9:
      out[k] = (f"multi-day · {by_order[gk]:,.0f} in is over the day line ({max_line:,.0f}) · "
                f"proposed at a share when its turn in the pool comes")
  return out


def _upsert_proposed_line(cursor, plan_date, order, rel, item, rows, agg, tier,
                          reason, actor):
  """Insert (or revive a Removed) line as Proposed by the agent. Returns
  False if PC already has the line active — the proposal records it, the
  plan is left alone."""
  cursor.execute("""
      SELECT line_status FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
  """, (plan_date, order, rel, item))
  ex = cursor.fetchone()
  if ex and ex['line_status'] in _ACTIVE_STATUSES:
    return False
  # priority_note is VARCHAR(200); the proposal reason may run to 500 now
  # that v1.2 appends the SHORT clause (2026-09-18 review). Truncating
  # here keeps the full text on pc_agent_proposal_line.reason.
  note = (reason or '')[:200] or None
  if ex:   # Removed earlier today — revive under the PK
    _delete_source_rows(cursor, plan_date, order, rel, item)
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET nx_sku = %s, qty_planned = %s, ship_by_date = %s, shipto_code = %s,
            carrier = %s, load_inches = %s, load_minutes = %s,
            surcharge_minutes = %s, line_status = 'Proposed',
            added_by_role = 'Agent', tier = %s, priority_reason = NULL,
            priority_note = %s, proposed_by = %s, snooze_to_date = NULL,
            updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (agg['sku'], agg['qty'], agg['ship_by'], agg['shipto'], agg['carrier'],
          agg['inches'], agg['minutes'], agg['surcharge'],
          tier, note, actor, actor, plan_date, order, rel, item))
    _insert_source_rows(cursor, plan_date, order, rel, item, rows)
  else:
    _insert_line_and_source(cursor, plan_date, order, rel, item, rows, agg,
                            role='Agent', actor=actor, tier=tier, reason=None)
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Proposed', proposed_by = %s, priority_note = %s
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (actor, note, plan_date, order, rel, item))
  return True


@anvil.server.callable
def ship_plan_agent_dry_run(plan_date=None):
  """What the engine WOULD propose for plan_date, treating the day as empty.
  Writes nothing. 2026-10-12."""
  return _agent_propose_impl(plan_date, force=True, dry_run=True)


@anvil.server.callable
def ship_plan_agent_compare(plan_date=None):
  """Text diff: the latest REAL proposal for the day vs a dry run of the
  current engine (X08 gate, stalled holds, two-orders-one-shelf). For the
  server console: print(ship_plan_agent_compare('2026-10-12')). 2026-10-12."""
  pd = _parse_date(plan_date) if plan_date else _next_business_day()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP 1 proposal_id, agent_version, generated_at, proposed_inches, capacity_inches
        FROM dbo.pc_agent_proposal WHERE plan_date = %s ORDER BY generated_at DESC
    """, (pd,))
    p = cursor.fetchone()
    old = {}
    if p:
      cursor.execute("""
          SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, rank_no, proposed_tier,
                 load_inches, reason
          FROM dbo.pc_agent_proposal_line WHERE proposal_id = %s AND rank_no > 0
      """, (p['proposal_id'],))
      for r in cursor.fetchall():
        old[(r['nx_order_numb'], r['nx_rel_numb'], str(r['nx_item_no']))] = r
  finally:
    conn.close()
  new = ship_plan_agent_dry_run(pd)
  if new.get('status') != 'success':
    return f"dry run failed: {new.get('message')}"
  newl = {(l['order'], l['rel'], str(l['item'])): l for l in new['lines']}
  out = [f"PLAN {pd}  -  morning proposal {('#%s %s at %s' % (p['proposal_id'], p['agent_version'], str(p['generated_at'])[:16])) if p else '(none)'}"
         f"  vs  dry run {_AGENT_VERSION} now (day treated as empty)",
         f"  morning: {len(old)} lines, {float(p['proposed_inches'] or 0) if p else 0:,.0f} in"
         f"   |   now: {len(newl)} lines, {new['proposed_inches']:,.0f} in of {new['target_inches']:,.0f} target; "
         f"gated {new['n_gated']}, short {new['n_short']} (stock {new.get('n_stock_short', 0)}), "
         f"partials {new.get('n_stock_partial', 0)}, shelf decisions {new.get('n_shelf_decision', 0)}, "
         f"split-needed {new['n_split_needed']}, blocked {new['n_blocked']}, "
         f"multi-day {new.get('n_multiday', 0)} ({new.get('multiday_used_inches', 0):,.0f} of "
         f"{new.get('multiday_pool_inches', 0):,.0f} in pool), waiting {new.get('n_multiday_waiting', 0)}"]
  for g in new.get('multiday') or []:                       # v1.5
    out.append(f"  MULTI-DAY order {g['order']}: day {g['day']} of about {g['about']}"
               f"{' (started)' if g.get('started') else ''}, {g['tier']}, {g['took_inches']:,.0f} in today, "
               f"{g['remaining_inches_after']:,.0f} in left after; assemblers {g.get('assemblers') or 'none yet'}"
               f"{'; ships when complete' if g.get('ship_complete') else ''}")
    for l in g['lines']:
      out.append(f"      {l['line']} / {l['sku']:<14} {l['qty_today']:g} of {l['qty_full']:g}"
                 f" (prior {l['qty_prior']:g}) {l['load_inches']:7,.1f} in")
    if g.get('built_est'):
      out.append(f"      built (tablet Complete or earlier shares), at 0 in: {', '.join(g['built_est'])}")
    if g.get('waiting'):
      out.append(f"      not today (pool used): {', '.join(g['waiting'])}")
  for w in new.get('multiday_waiting') or []:
    out.append(f"  MULTI-DAY order {w['order']} waits ({w['load_inches']:,.0f} in): {w['why']}")
  dropped = [k for k in old if k not in newl]
  added = [k for k in newl if k not in old]
  kept = [k for k in old if k in newl]
  short_by = {(s['order'], str(s['item'])): s for s in new.get('short', [])}
  gated_by = {(g['order'], str(g['item'])): g for g in new.get('gated', [])}
  def _why(k):
    s = short_by.get((k[0], k[2]))
    if s:
      return f"short: {s.get('note') or ''}".strip()
    g = gated_by.get((k[0], k[2]))
    if g:
      return f"gated {g.get('gates') or ''}: {(g.get('notes') or [''])[0][:70]}"
    return 'not reached / blocked / split-needed'
  out.append(f"\nDROPPED since this morning ({len(dropped)}):")
  for k in sorted(dropped, key=lambda k: old[k]['rank_no']):
    r = old[k]
    out.append(f"  #{r['rank_no']:<3} {k[0]}-{k[1]}-{k[2]} / {r['nx_sku']:<14} {r['proposed_tier']:<7} "
               f"{float(r['load_inches'] or 0):7,.1f} in  -> {_why(k)}")
  out.append(f"\nADDED by the dry run ({len(added)}):")
  for k in sorted(added, key=lambda k: newl[k]['rank']):
    l = newl[k]
    out.append(f"  #{l['rank']:<3} {l['line']} / {l['sku']:<14} {l['tier']:<7} {l['load_inches']:7,.1f} in  "
               f"{('(partial %g of %g)' % (l['qty_planned'], l['qty_full'])) if l.get('qty_planned') else ''}"
               f"{(' multi-day ' + l['multiday']) if l.get('multiday') else ''}"
               f"{' (built, 0 in)' if l.get('multiday_built_est') else ''}"
               f"{('(SHELF DECISION - %s has the stock)' % l.get('decision_taker')) if l.get('shelf_decision') else ''}")
  changed = [k for k in kept if (newl[k].get('qty_planned') or newl[k].get('shelf_decision')
                                 or old[k]['proposed_tier'] != newl[k]['tier'])]
  out.append(f"\nKEPT ({len(kept)}), of which changed ({len(changed)}):")
  for k in sorted(changed, key=lambda k: newl[k]['rank']):
    l, r = newl[k], old[k]
    bits = []
    if r['proposed_tier'] != l['tier']:
      bits.append(f"tier {r['proposed_tier']} -> {l['tier']}")
    if l.get('qty_planned'):
      bits.append(f"partial {l['qty_planned']:g} of {l['qty_full']:g} ({float(r['load_inches'] or 0):,.1f} -> {l['load_inches']:,.1f} in)")
    if l.get('shelf_decision'):
      bits.append(f"SHELF DECISION at 0 in - {l.get('decision_taker')} has the stock")
    out.append(f"  {l['line']} / {l['sku']:<14} " + '; '.join(bits))
  if new.get('short'):
    out.append(f"\nSHORT lines named by the dry run ({len(new['short'])}):")
    for s in new['short']:
      out.append(f"  {s['line']} / {s['sku']:<14} of order {s.get('short_of')}: {s.get('note') or ''}")
  return '\n'.join(out)


@anvil.server.background_task
def agent_propose_task(plan_date=None, force=False):
  """Background task wrapper: the fill can take longer than Anvil's 30 s
  server-call limit (first live run, 2026-09-06: ~1,100 candidates)."""
  return _agent_propose_impl(plan_date, force)


@anvil.server.callable
def ship_plan_agent_propose(plan_date=None, force=False):
  """Launch the proposal run in the background; poll with
  ship_plan_agent_task_status(task_id)."""
  task = anvil.server.launch_background_task('agent_propose_task',
                                             plan_date, force)
  return {'status': 'started', 'task_id': task.get_id()}


@anvil.server.callable
def ship_plan_agent_task_status(task_id):
  try:
    t = anvil.server.get_background_task(task_id)
    if t.is_completed():
      return {'status': 'done', 'result': t.get_return_value()}
    return {'status': 'running', 'state': t.get_state() or {}}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}


# dry run only: the body of dbo.vw_pc_ship_candidates (item grain, 066) with
# ONE change - lines on the plan_date being dry-run are not suppressed, and
# (v1.6.2) neither are TODAY's: a dry run for tomorrow must see today's
# started multi-day lines as the 06:30 run will (10-26: 602392 was invisible).
# Keep in step with the view if its column list changes. Params: plan_date, end.
_DRY_CANDIDATES_SQL = """
    SELECT  r.nx_order_numb, r.nx_rel_numb, r.nx_item_no, r.nx_sku,
            r.nx_sku_desc AS sku_desc, r.nx_part_line AS part_line, r.bucket,
            r.nx_qty_ordered AS qty_open, r.nx_qty_balance_due AS qty_balance,
            CAST(COALESCE(r.nx_dship_by_date, r.nx_hship_by_date) AS DATE) AS ship_by_date,
            r.nx_shipto_code AS shipto_code, r.nx_carrier AS carrier,
            1 AS n_source_rows,
            CASE WHEN r.element_class = 'Non-Standard' THEN 1 ELSE 0 END AS n_non_standard,
            CAST(r.class_was_defaulted AS INT) AS n_class_defaulted,
            r.minutes_charged AS load_minutes, r.surcharge_minutes AS surcharge_minutes,
            r.work_inches AS load_inches, r.weight_set_id AS weight_set_id,
            r.nx_last_updated AS feed_last_updated
    FROM dbo.vw_pc_ship_candidate_rows r
    WHERE NOT EXISTS (
            SELECT 1 FROM dbo.pc_ship_plan_line pl
            WHERE pl.nx_order_numb = r.nx_order_numb
              AND pl.nx_rel_numb   = r.nx_rel_numb
              AND pl.nx_item_no    = r.nx_item_no
              AND pl.line_status IN ('Planned','Snoozed','Proposed','Confirmed')
              AND pl.plan_date > CAST(GETDATE() AS DATE)
              AND pl.plan_date <> %s)
"""


def _agent_propose_impl(plan_date=None, force=False, dry_run=False):
  """Build the agent's proposal for a day (default: next business day).
  Deterministic: ungated candidates within the horizon, in priority order
  (score, oldest ship-by, biggest load); fill Must/Next up to
  AGENT_FILL_PCT of capacity ON TOP of whatever PC already planned; then
  up to AGENT_STRETCH_LINES more groups as Stretch - v1.4.1 (2026-10-13):
  the Stretch tier as a WHOLE must fit the buffer the fill left (each
  group used to be tested alone, so five groups of 200-400 in rode on a
  496 in buffer - '+1,385 if time' on 10-13).
  Tier rule: Must = late (A01) or due today/tomorrow (A02); else Next.
  v1.5 (2026-10-25): an order over the max-line test is a MULTI-DAY order,
  proposed at a day-sized share out of a pool (AGENT_MULTIDAY_SHARE_PCT of
  the fill target); started orders first; progress estimated from the
  shares on earlier published plans until the tablet count (step 2).
  Re-running supersedes the earlier proposal and clears ONLY its
  still-Proposed lines — anything PC confirmed stays and counts.
  Refuses a Published day unless force. Takes a proposal-time flag
  snapshot under revision_no = -1 (publish uses 0+)."""
  actor = _AGENT_ACTOR
  plan_date = _parse_date(plan_date) if plan_date else _next_business_day()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    ap = _agent_params(cursor)
    if not ap['enabled'] and not force:
      return {'status': 'skipped', 'message': 'AGENT_ENABLED is 0.'}
    cursor.execute("SELECT plan_status, weight_set_id FROM dbo.pc_ship_plan "
                   "WHERE plan_date = %s", (plan_date,))
    hdr = cursor.fetchone()
    if hdr and hdr['plan_status'] == 'Published' and not force and not dry_run:
      return {'status': 'skipped',
              'message': f'{plan_date} is already published.'}
    # 2026-09-07 (Labor Day): no roster -> 0 capacity -> a 0-line proposal
    # and a text to four people. No capacity = no plan day; say so and stop
    # BEFORE touching the header or superseding anything.
    cap_probe = _capacity_for(cursor, plan_date, prefer_estimate=True)
    if float(cap_probe.get('capacity_inches_effective') or 0) <= 0 and not force:
      return {'status': 'skipped', 'plan_date': plan_date.isoformat(),
              'message': (f'{plan_date}: no capacity (holiday or nobody on the '
                          f'roster) - nothing proposed. {cap_probe.get("capacity_detail", "")}')}
    if not dry_run:
      _ensure_header(cursor, plan_date, actor)
    cursor.execute("SELECT weight_set_id FROM dbo.pc_ship_plan "
                   "WHERE plan_date = %s", (plan_date,))
    weight_set_id = (cursor.fetchone() or {}).get('weight_set_id')

    # --- supersede an earlier run: its untouched Proposed lines go ----
    n_cleared = 0
    if not dry_run:                       # a dry run supersedes nothing
      cursor.execute("""
          UPDATE dbo.pc_agent_proposal SET status = 'SUPERSEDED'
          WHERE plan_date = %s AND status = 'PROPOSED'
      """, (plan_date,))
      # 2026-09-25: the rush lane (091) proposes with the same actor and can
      # land seconds before this fill (06:30:04 vs 06:30:38 today) - its lines
      # carry priority_reason RUSH and are PC's to keep; the fill treats them
      # like anything already planned (they count in `used`, upsert skips them).
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET line_status = 'Removed', updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND line_status = 'Proposed' AND proposed_by = %s
            AND ISNULL(priority_reason, '') <> 'RUSH'
      """, (actor, plan_date, actor))
      n_cleared = cursor.rowcount

    # --- capacity + what PC already planned --------------------------
    cap = _capacity_for(cursor, plan_date, prefer_estimate=True)
    cap_in = float(cap.get('capacity_inches_effective') or 0)
    target = cap_in * ap['fill_pct']
    used = 0.0 if dry_run else float(_planned_inches(cursor, plan_date) or 0)
    already_in = used

    # --- candidates in the horizon, ranked exactly like the pane -----
    end = _horizon_end(ap['horizon_days'])
    # dry run: vw_pc_ship_candidates hides every line already on a plan from
    # today on - including the day being dry-run. The day is treated as
    # EMPTY, so its own lines must be candidates again: same view body, the
    # plan_date exempt from the already-planned guard (_DRY_CANDIDATES_SQL).
    cand_src = ('(' + _DRY_CANDIDATES_SQL + ')') if dry_run else 'dbo.vw_pc_ship_candidates'
    cand_args = ((plan_date, end) if dry_run else (end,))
    cursor.execute("""
        SELECT c.*, ISNULL(fs.is_gated, 0) AS is_gated, fs.gate_codes,
               ISNULL(fs.priority_score, 0) AS priority_score, fs.flag_codes
        FROM """ + cand_src + """ c
        LEFT JOIN dbo.vw_pc_order_flag_scores fs
               ON fs.nx_order_numb = c.nx_order_numb AND fs.nx_rel_numb = c.nx_rel_numb
              AND fs.nx_item_no = c.nx_item_no
        WHERE c.ship_by_date <= %s
        ORDER BY ISNULL(fs.is_gated, 0), ISNULL(fs.priority_score, 0) DESC,
                 c.ship_by_date, c.load_inches DESC, c.nx_order_numb, c.nx_rel_numb, c.nx_item_no
    """, cand_args)
    cands = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT f.nx_order_numb, f.nx_rel_numb, f.nx_item_no, f.flag_code, f.note
        FROM dbo.vw_pc_order_flags f
        JOIN """ + cand_src + """ c
          ON c.nx_order_numb = f.nx_order_numb AND c.nx_rel_numb = f.nx_rel_numb
         AND c.nx_item_no = f.nx_item_no
        WHERE c.ship_by_date <= %s
        ORDER BY f.flag_code
    """, cand_args)
    notes = {}
    for r in cursor.fetchall():
      notes.setdefault((r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']), []).append(r)
    # All source rows for the horizon in ONE query (per-line view queries
    # were the 30 s timeout on the first run). 066: one row per e21 line.
    cursor.execute("""
        SELECT * FROM dbo.vw_pc_ship_candidate_rows
        WHERE CAST(COALESCE(nx_dship_by_date, nx_hship_by_date) AS DATE) <= %s
    """, (end,))
    rows_by_key = {}
    for r in cursor.fetchall():
      rows_by_key.setdefault((r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']), []).append(r)

    # --- the fill: WHOLE-ORDER GROUPS (rule, Dave 2026-09-13) ---------
    # If one line of a sales order is proposed, every other open line
    # with the SAME detail ship-by date is proposed with it. Assembly
    # techs run each order as a mini-project; a partial order on the
    # board is chaos. So the ATOM is the group (order, ship_by_date):
    #   - zero-touch siblings RIDE ALONG (visible at zero). A GATED
    #     sibling rides ONLY when its gate is X06 with stock due in between
    #     today and the plan date - the order can still ship complete
    #     (planned-short rule, Dave 2026-09-18). Any other gate (X01 hold,
    #     X02 no stock, X03 committed elsewhere, X05 material hold, X06
    #     overdue / due later / no ETA) leaves the line OFF the plan and the order
    #     is proposed SHORT - every driver's reason names the line, so
    #     the plan never says 'build 23' when 22 can be built. Visible is
    #     enough; no confirmation step. The check runs every morning, so
    #     a slipped ETA drops the line the next run. Neither kind is
    #     picked on its own: a group needs a DRIVER (ungated, non-zero
    #     line) to be proposed at all.
    #   - a sibling PC snoozed, or planned on another day, BLOCKS the
    #     group - PC already decided; reported, never overridden.
    #   - a ship-complete order (nx_order_type 'S') with a SHORT sibling
    #     is BLOCKED: it cannot go short, nothing on it ships until the
    #     gate clears (an X06 due in by the plan date is not short).
    #   - a group bigger than the whole day, or than AGENT_MAX_LINE_PCT
    #     of the fill target, is a MULTI-DAY order: proposed at a day-sized
    #     share out of the multi-day pool (v1.5, below). With the pool off
    #     (AGENT_MULTIDAY_SHARE_PCT 0) it is a SPLIT DECISION for PC.
    #   - first-fit by the group's best ungated line (the candidate order
    #     already is score DESC, ship-by, load DESC); tier by its drivers
    #     (Must = A01/A02 on any driver, else Next). Stretch is decided
    #     AFTER the fill against the final slack - whole groups only.
    max_line = target * ap['max_line_pct']
    cursor.execute("""
        SELECT pl.nx_order_numb, pl.nx_rel_numb, pl.nx_item_no, pl.nx_sku, pl.plan_date,
               pl.line_status, pl.snooze_to_date
        FROM dbo.pc_ship_plan_line pl
        WHERE (pl.plan_date <> %s AND pl.plan_date > CAST(GETDATE() AS DATE)
               -- v1.6.3: 'another day' = a FUTURE day. A line on TODAY's plan is not
               -- elsewhere when a later day is planned (dry run / afternoon Run agent for
               -- tomorrow: 60 lines blocked on 10-26, 602392 among them); at 06:30 the
               -- plan day IS today, so the real run reads the same as before.
               AND pl.line_status IN ('Planned', 'Proposed', 'Confirmed'))
           OR (pl.line_status = 'Snoozed'
               AND ISNULL(pl.snooze_to_date, pl.plan_date) >= CAST(GETDATE() AS DATE))
    """, (plan_date,))
    elsewhere = {(r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']): r
                 for r in cursor.fetchall()}
    cursor.execute("""
        SELECT DISTINCT nx_order_numb FROM dbo.sales_orders_due
        WHERE RTRIM(nx_order_type) = 'S'
    """)
    ship_complete = {r['nx_order_numb'] for r in cursor.fetchall()}

    # --- v1.3 TWO ORDERS, ONE SHELF (decision 7, 2026-10-10) ----------
    # Every gate looks at ONE line against the shelf. Two Must lines of
    # the same item that each fit on their own can still outrun the shelf
    # together (10-07: YS80TS0075 400 + 1,400 planned, 1,598 on hand).
    # The fill below keeps a running count per item, in priority order:
    # what the shelf still covers is proposed (a partial carries SPLIT n
    # of N and names the order that has the rest); nothing left = the
    # line goes SHORT like a gated sibling. Stock items only (e21 item
    # type other than N); an item with no inventory row is left alone.
    # Lines PC already has on this day count first - they were decided.
    # v1.3.2 (2026-10-12 pm): e21 ALLOCATION IS IGNORED on purpose. Item
    # status 14 = Allocated is bookkeeping (e21 hands all of on-hand to
    # whichever lines got there first - 160 YS61S0400 to an order due
    # 10-30 while a late one waited); PC re-allocates as the plan needs,
    # and allocation moves to the shipping step in the production app
    # (Dave). A v1.3.1 that honoured it went live for one dry run only:
    # 36 lines short behind later-due orders. On-hand is the shelf.
    cursor.execute("""
        SELECT d.nx_order_numb, d.nx_rel_numb, d.nx_item_no,
               LTRIM(RTRIM(ISNULL(d.nx_item_type, ''))) AS item_type,
               LTRIM(RTRIM(d.nx_sku)) AS sku, i.on_hand
        FROM dbo.sales_orders_due d
        LEFT JOIN dbo.inventory_current i
               ON i.sku = d.nx_sku AND i.cost_center = 'Starlite'
        WHERE CAST(COALESCE(d.nx_dship_by_date, d.nx_hship_by_date) AS DATE) <= %s
    """, (end,))
    shelf, line_stock = {}, {}
    for r in cursor.fetchall():
      line_stock[(r['nx_order_numb'], r['nx_rel_numb'], str(r['nx_item_no']))] = \
          (r['item_type'], r['sku'], r['on_hand'])
      if r['on_hand'] is not None:
        shelf[r['sku']] = float(r['on_hand'])
    stock_used, stock_taker = {}, {}
    cursor.execute("""
        SELECT LTRIM(RTRIM(nx_sku)) AS sku, nx_order_numb, SUM(qty_planned) AS q
        FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND line_status IN %s
        GROUP BY LTRIM(RTRIM(nx_sku)), nx_order_numb
    """, (plan_date, _COUNTS_AGAINST_CAP))
    for r in ([] if dry_run else cursor.fetchall()):
      stock_used[r['sku']] = stock_used.get(r['sku'], 0.0) + float(r['q'] or 0)
      stock_taker.setdefault(r['sku'], r['nx_order_numb'])

    def _shelf_check(m):
      """None = no rule for this line; (avail, taker) otherwise."""
      key = (m['nx_order_numb'], m['nx_rel_numb'], str(m['nx_item_no']))
      item_type, sku, on_hand = line_stock.get(key, ('', None, None))
      if item_type == 'N' or on_hand is None or not sku:
        return None
      return (shelf.get(sku, 0.0) - stock_used.get(sku, 0.0), stock_taker.get(sku), sku)

    def _gkey(c):
      return (c['nx_order_numb'], str(c.get('ship_by_date') or '')[:10])
    groups, group_order = {}, []
    for c in cands:
      k = _gkey(c)
      if k not in groups:
        groups[k] = []
        group_order.append(k)
      groups[k].append(c)

    # --- v1.5 MULTI-DAY ORDERS (decisions 1-8, 2026-10-25) --------------
    # The day's multi-day POOL is AGENT_MULTIDAY_SHARE_PCT of the fill
    # target (40% = ~794 in at 2,482 cap). Started orders - a share on an
    # earlier PUBLISHED plan - go FIRST (decision 7); the pool is used in
    # that order, so the first order takes it and the next gets what is
    # left (usually nothing: one multi-day order at a time in practice).
    md_pool = target * ap['multiday_share_pct']
    md_used, md_taker, md_out, md_waiting = 0.0, None, [], []
    md_prior = _md_prior_shares(cursor, plan_date) if md_pool > 0 else {}
    md_orders = [gk[0] for gk in group_order
                 if any(_mdkey(m) in md_prior for m in groups[gk])
                 or sum(float(m.get('load_inches') or 0) for m in groups[gk]) > max_line]
    # v1.5.1: the tablet is the fact - Complete = built, an open Start =
    # started, Count taps = pieces done; the prior-share estimate fills in
    # where the tablet is silent.
    md_tab = _md_tablet_facts(cursor, md_orders) if md_pool > 0 else {}
    def _md_started(gk):
      return any(_mdkey(m) in md_prior or (md_tab.get(_mdkey(m)) or {}).get('state') in ('running', 'paused', 'done')
                 for m in groups[gk])
    if md_prior or md_tab:
      group_order.sort(key=lambda gk: 0 if _md_started(gk) else 1)
    md_asm = _md_assemblers(cursor, md_orders) if md_pool > 0 else {}
    md_modes = _md_order_modes(cursor, plan_date) if md_pool > 0 else {}   # v1.6
    md_plan = _md_plan_rows(cursor, plan_date, md_orders) if md_pool > 0 else {}   # v1.7 (121)
    asm_day = _md_assembler_day(cursor, cap) if md_pool > 0 else 0.0

    picks, deferred, gated, split_needed, blocked = [], [], [], [], []
    short_lines = []
    skipped_zero = n_groups = 0
    for k in group_order:
      members = groups[k]
      order = k[0]
      drivers = [m for m in members
                 if not m.get('is_gated')
                 and (float(m.get('load_inches') or 0) > 0 or ap['include_zero_touch'])]
      if not drivers:
        if all(m.get('is_gated') for m in members):
          gated.extend(members)
        else:
          skipped_zero += len(members)   # pure pick-and-ship order: not the agent's to propose
        continue
      # planned-short rule (v1.2): split the group into the lines that
      # ride (ungated, or X06 due in by the plan date) and the SHORT
      # lines (any other gate) that stay off the plan.
      riders, short = [], []
      for m in members:
        if not m.get('is_gated'):
          riders.append(m)
          continue
        lnotes = notes.get((order, m['nx_rel_numb'], m['nx_item_no']), [])
        eta = _due_in_eta(lnotes)
        # an ETA already in the past with on-hand still 0 is a slipped PO,
        # not stock on the way (X06 says OVERDUE): that line goes short.
        if eta is not None and date.today() <= eta <= plan_date:
          m['due_in'] = eta.isoformat()
          riders.append(m)
        else:
          xn = [n['note'] for n in lnotes if (n['flag_code'] or '')[:1] == 'X']
          m['short_note'] = xn[0] if xn else ''
          short.append(m)
      # a ship-complete order (nx_order_type 'S') cannot go short: nothing
      # on it ships until the gate clears. Checked BEFORE the elsewhere
      # test so the block reason names the real cause.
      if short and order in ship_complete:
        why = (f"ship-complete order; {_line_label(order, short[0]['nx_rel_numb'], short[0]['nx_item_no'], short[0]['nx_sku'])} "
               f"gated {short[0].get('gate_codes') or ''}").strip()
        for m in members:
          m['block_reason'] = why
        blocked.extend(members)
        continue
      short_txt = ''
      if short:
        short_txt = ('SHORT ' + str(len(short)) + (' line' if len(short) == 1 else ' lines')
                     + ' off the plan: ' + '; '.join(_short_clause(m) for m in short))[:200]
        for m in short:
          m['short_of'] = order
        short_lines.extend(short)
      # v1.3: the shelf. Riders whose item is already spoken for on this
      # plan go SHORT (stock planned on order X) or get a partial quantity.
      # v1.4 (Production, 10-12): a line whose shelf is already spoken for
      # on this plan is NOT dropped - it rides at ZERO inches with a SHELF
      # DECISION note (visible, not counted). PC + Sales decide before
      # publish: Remove it (ship the other order complete) or plan a
      # quantity on each (decision 5). Undecided at publish = the priority
      # pick stands (ship_plan_publish removes it, logged). A ship-complete
      # order cannot go partial, so it stays BLOCKED as before.
      keep = []
      for m in riders:
        chk = _shelf_check(m)
        need = float(m.get('qty_open') or 0)
        if chk is None or need <= 0 or m.get('is_gated'):
          keep.append(m)            # gated riders (X06 due in) are the planned-short rule's, not the shelf's
          continue
        avail, taker, sku = chk
        if avail >= need - 1e-9:
          keep.append(m)
          continue
        stock_qty = float(int(avail)) if avail > 0 else 0.0
        if stock_qty < 1:
          if order in ship_complete:
            m['short_note'] = (f"stock planned on order {taker}" if taker else 'no stock left on the shelf')
            m['short_of'] = order
            m['stock_short'] = True
            short.append(m)
            continue
          m['shelf_decision'] = True
          m['decision_taker'] = taker
          m['decision_load'] = float(m.get('load_inches') or 0)
          m['load_inches'] = 0.0
          keep.append(m)
          continue
        # a partial: what the shelf still covers
        m['stock_qty'] = stock_qty
        f = m['stock_qty'] / need
        m['stock_full_qty'] = need
        m['stock_taker'] = taker
        m['load_inches'] = round(float(m.get('load_inches') or 0) * f, 2)
        keep.append(m)
      if short and not any(m is d for d in drivers for m in keep):
        # the driver itself lost the shelf: the group is not proposed today
        for m in short:
          if m.get('stock_short'):
            short_lines.append(m)
        continue
      if order in ship_complete and any(m.get('stock_short') for m in short):
        s0 = next(m for m in short if m.get('stock_short'))
        why = (f"ship-complete order; {_line_label(order, s0['nx_rel_numb'], s0['nx_item_no'], s0['nx_sku'])} "
               f"{s0.get('short_note') or 'short on the shelf'}")
        for m in members:
          m['block_reason'] = why
        blocked.extend(members)
        continue
      if any(m.get('stock_short') for m in short):
        short_txt = ('SHORT ' + str(len(short)) + (' line' if len(short) == 1 else ' lines')
                     + ' off the plan: ' + '; '.join(_short_clause(m) for m in short))[:200]
        for m in short:
          if m.get('stock_short'):
            short_lines.append(m)
      members = keep
      load = sum(float(m.get('load_inches') or 0) for m in members)
      for m in members:
        m['group_inches'] = load
        m['group_n'] = len(members)
        m['group_short'] = short_txt
        m['rides_along'] = not any(m is d for d in drivers)
      blk = [m for m in members
             if (order, m['nx_rel_numb'], m['nx_item_no']) in elsewhere]
      if blk:
        e = elsewhere[(order, blk[0]['nx_rel_numb'], blk[0]['nx_item_no'])]
        when = str(e.get('snooze_to_date') or e.get('plan_date') or '')[:10]
        why = (f"sibling {_line_label(order, blk[0]['nx_rel_numb'], blk[0]['nx_item_no'], blk[0]['nx_sku'])} already "
               f"{'snoozed to' if e['line_status'] == 'Snoozed' else 'planned on'} {when}")
        for m in members:
          m['block_reason'] = why
        blocked.extend(members)
        continue
      codes = ' '.join((m.get('flag_codes') or '') for m in drivers)
      tier = 'Must' if ('A01' in codes or 'A02' in codes) else 'Next'
      # --- v1.5: an order over the max-line test is a MULTI-DAY order -
      # proposed at a day-sized share out of the pool, not left as a SPLIT
      # decision. A STARTED order stays on this path until done, whether or
      # not what remains still looks oversize. Progress until the tablet
      # count (step 2): remaining = ordered - shares on earlier published
      # days (assume they were built). Inside the order the lines with the
      # least remaining load go whole first; the first that does not fit
      # gets the rounded remainder (lot 50 / 25 / 10 / 1); the rest stay in
      # the candidates for a later day. A line whose shares already cover it
      # rides at ZERO load (visible, so the retro sees it the day the order
      # ships). Stretch never applies to a multi-day order (paper s10). The
      # board note starts 'SPLIT n of N · day d of about k' - machine-read.
      started = any(_mdkey(m) in md_prior for m in members) or (md_pool > 0 and _md_started(k))
      if md_pool > 0 and (started or load > cap_in + 1e-9 or load > max_line + 1e-9):
        gdays = set()
        for m in members:
          pr = md_prior.get(_mdkey(m))
          if pr:
            gdays |= set(pr['days'])
        day_no = len(gdays) + 1
        budget = max(0.0, min(md_pool - md_used, target - used))
        mp = md_plan.get(order) or {}
        planned_asm = mp.get('assemblers')
        if planned_asm and asm_day > 0:
          # v1.7: PC planned a headcount -> share = assemblers x one
          # assembler-day, never more than what is left of the pool
          budget = max(0.0, min(budget, planned_asm * asm_day))
        if not started and budget < 0.25 * md_pool and not planned_asm:
          # a NEW multi-day order does not start on the scraps of the pool
          # (a 17-piece day 1 is a trickle, not a start); it waits its turn
          why = (f"multi-day: waits for the pool - order {md_taker} has it today" if md_taker
                 else f"multi-day: no room in the pool today ({md_pool:,.0f} in, {budget:,.0f} left)")
          for m in members:
            m['split_reason'] = why
          md_waiting.append({'order': order, 'why': why, 'load_inches': round(load, 1),
                             'started': False, 'day': 1})
          split_needed.extend(members)
          continue
        work, rem_total_in = [], 0.0
        for m in members:
          need = float(m.get('stock_qty') or m.get('qty_open') or 0)
          li = float(m.get('load_inches') or 0)
          pr = md_prior.get(_mdkey(m))
          m['md_full'] = need
          m['md_prior'] = float(pr['qty']) if pr else 0.0
          m['md_days'] = sorted(pr['days']) if pr else []
          if need <= 0 or li <= 0:
            continue                          # zero-touch sibling: rides as today
          per_piece = li / need
          m['md_per_piece'] = per_piece
          t = md_tab.get(_mdkey(m)) or {}
          m['md_tablet'] = t
          if t.get('count_total'):
            remaining = max(0.0, need - float(t['count_total']))   # the count is the fact
            m['md_prior'] = float(t['count_total'])
          else:
            remaining = max(0.0, need - m['md_prior'])
          lp = (mp.get('lines') or {}).get((m['nx_rel_numb'], str(m['nx_item_no']))) or {}
          pc_done = bool(mp.get('done')) or bool(lp.get('done'))
          if t.get('state') == 'done' or pc_done or remaining < 1:
            m['md_done'] = True               # built (tablet / PC) or shares cover it: visible at 0 in
            m['md_done_fact'] = (t.get('state') == 'done')
            m['md_done_pc'] = pc_done and t.get('state') != 'done'
            m['load_inches'] = 0.0
            continue
          m['md_share_override'] = lp.get('share')     # v1.7: PC typed pieces for this line today
          work.append((m, remaining, per_piece))
          rem_total_in += remaining * per_piece
        work.sort(key=lambda t: t[1] * t[2])  # least remaining load first
        take_in, taken, cut = 0.0, [], False
        for m, remaining, per_piece in work:
          left = budget - take_in
          if m.get('md_share_override') is not None:
            pieces = min(remaining, float(m['md_share_override']))   # v1.7: PC's number wins the arithmetic
            if pieces < 1:
              m['md_wait'] = True
              continue
            m['md_qty'] = pieces
            m['load_inches'] = round(pieces * per_piece, 2)
            take_in += m['load_inches']
            taken.append(m)
            continue
          if cut:
            m['md_wait'] = True               # one cut line per day; the rest wait
            continue
          if remaining * per_piece <= left + 1e-9:
            pieces = remaining
          else:
            cut = True
            pieces = _md_round(left / per_piece)
            if remaining - pieces < _md_lot(remaining) and remaining * per_piece <= left * 1.15:
              pieces = remaining              # no tail smaller than a lot
            if taken and pieces * per_piece < 0.10 * md_pool:
              pieces = 0.0                    # a scrap after whole lines is not a day's work
            if pieces < 1 and not taken and started:
              pieces = min(remaining, float(_md_lot(remaining)))   # a started order never stands still
          if pieces < 1:
            m['md_wait'] = True               # stays in the candidates today
            continue
          m['md_qty'] = pieces
          m['load_inches'] = round(pieces * per_piece, 2)
          take_in += m['load_inches']
          taken.append(m)
        est_done = [m for m in members if m.get('md_done') and not m.get('md_done_fact') and not m.get('md_done_pc')]
        if not taken and not work and est_done:
          # every line's shares are planned (ESTIMATE, no tablet Complete),
          # nothing shipped: one lot of the biggest such line keeps the order
          # in front of PC - check the floor. A tablet Complete needs no
          # check: the line rides at 0 in as built (v1.5.2, 597535 10-25).
          m = max(est_done, key=lambda m: float(m['md_full']))
          m['md_done'] = False
          m['md_check'] = True
          m['md_qty'] = min(float(m['md_full']), float(_md_lot(m['md_full'])))
          m['load_inches'] = round(m['md_qty'] * float(m.get('md_per_piece') or 0), 2)
          take_in += m['load_inches']
          taken.append(m)
        if not taken and not any(m.get('md_done') for m in members):
          why = (f"multi-day: waits for the pool - order {md_taker} has it today" if md_taker
                 else f"multi-day: no room in the pool today ({md_pool:,.0f} in, {budget:,.0f} left)")
          for m in members:
            m['split_reason'] = why
          md_waiting.append({'order': order, 'why': why, 'load_inches': round(load, 1),
                             'started': started, 'day': day_no})
          split_needed.extend(members)
          continue
        day_rate = (min(md_pool, planned_asm * asm_day) if (planned_asm and asm_day > 0) else md_pool)
        about = (day_no - 1) + max(1, int(-(-rem_total_in // day_rate))) if day_rate > 0 else day_no
        for m in members:
          m['multiday'] = True
          m['md_day'] = day_no
          m['md_about'] = about
          m['md_assemblers'] = md_asm.get(order)
          m['md_planned_asm'] = planned_asm
          m['md_mode'] = md_modes.get(order)
          m['md_started'] = started
          m['group_inches'] = take_in
        proposed = [m for m in members if not m.get('md_wait')]
        picks.extend((m, tier) for m in proposed)
        used += take_in
        md_used += take_in
        if take_in > 0:
          md_taker = md_taker or order          # v1.5.3: a built order (0 in) holds nothing
        n_groups += 1
        for m in taken:                          # book the shelf for today's pieces only
          chk = _shelf_check(m)
          if chk is not None:
            stock_used[chk[2]] = stock_used.get(chk[2], 0.0) + float(m['md_qty'])
            stock_taker.setdefault(chk[2], order)
        md_out.append({'order': order, 'day': day_no, 'about': about, 'started': started,
                       'assemblers': md_asm.get(order), 'assemblers_planned': planned_asm,
                       'assembler_day_in': round(asm_day, 1), 'tier': tier,
                       'took_inches': round(take_in, 1),
                       'remaining_inches_after': round(max(0.0, rem_total_in - take_in), 1),
                       'ship_complete': order in ship_complete,
                       'lines': [{'line': _line_label(order, m['nx_rel_numb'], m['nx_item_no']),
                                  'sku': m['nx_sku'], 'qty_today': float(m['md_qty']),
                                  'qty_full': float(m['md_full']), 'qty_prior': float(m['md_prior']),
                                  'load_inches': float(m['load_inches'])} for m in taken],
                       'built_est': [_line_label(order, m['nx_rel_numb'], m['nx_item_no'])
                                     for m in members if m.get('md_done')],
                       'waiting': [_line_label(order, m['nx_rel_numb'], m['nx_item_no'])
                                   for m in members if m.get('md_wait')]})
        continue
      if load > cap_in + 1e-9 or load > max_line + 1e-9:     # multi-day pool off (v1.4 rule)
        why = ('order bigger than the whole day' if load > cap_in
               else f"order over {int(ap['max_line_pct'] * 100)}% of the fill target")
        for m in members:
          m['split_reason'] = why
        split_needed.extend(members)
        continue
      if used + load <= target + 1e-9:
        picks.extend((m, tier) for m in members)
        used += load
        n_groups += 1
        for m in members:                       # v1.3: book the shelf
          chk = _shelf_check(m)
          if chk is not None and not m.get('shelf_decision'):   # v1.4: a decision line takes nothing
            sku = chk[2]
            stock_used[sku] = stock_used.get(sku, 0.0) + float(m.get('stock_qty') or m.get('qty_open') or 0)
            stock_taker.setdefault(sku, order)
      else:
        deferred.append(members)
    slack = cap_in - used
    stretch, n_stretch_groups, stretch_in = [], 0, 0.0
    for members in deferred:
      if n_stretch_groups >= ap['stretch_lines']:
        break
      g = float(members[0].get('group_inches') or 0)
      if g <= slack - stretch_in + 1e-9:      # v1.4.1: the tier fits the buffer TOGETHER
        stretch.extend((m, 'Stretch') for m in members)
        n_stretch_groups += 1
        stretch_in += g
    skipped = sum(len(g) for g in deferred) - len(stretch)
    oversize = split_needed          # name kept for the response below

    # --- record the proposal ----------------------------------------
    proposal_id = None
    if not dry_run:
      cursor.execute("""
          INSERT INTO dbo.pc_agent_proposal
              (plan_date, agent_version, capacity_inches, proposed_inches, status)
          OUTPUT INSERTED.proposal_id
          VALUES (%s, %s, %s, %s, 'PROPOSED')
      """, (plan_date, _AGENT_VERSION, round(cap_in, 1), round(used, 1)))
      proposal_id = cursor.fetchone()['proposal_id']

    out_lines, rank, n_added = [], 0, 0
    for c, tier in picks + stretch:
      rank += 1
      order, sku = c['nx_order_numb'], c['nx_sku']
      rel, item = c['nx_rel_numb'], c['nx_item_no']
      key = (order, rel, item)
      due = str(c.get('ship_by_date') or '')[:10]
      if c.get('rides_along'):
        how = ((f"stock due in {c.get('due_in')}, gated {c.get('gate_codes') or ''}".strip()
                if c.get('due_in') else f"gated {c.get('gate_codes') or ''}".strip())
               if c.get('is_gated') else 'zero-touch')
        reason = (f"{tier}: rides with order {order} — whole-order rule, due {due} "
                  f"({how}) — {float(c.get('load_inches') or 0):,.1f} in")[:500]
      else:
        reason = _agent_reason(c, tier, notes.get(key, []))
        if int(c.get('group_n') or 1) > 1:
          reason = (reason + f" · order {int(c['group_n'])} lines, "
                    f"{float(c.get('group_inches') or 0):,.0f} in together")[:500]
      if c.get('group_short'):
        reason = (reason + ' · ' + c['group_short'])[:500]
      if c.get('shelf_decision'):
        # v1.4: the board note must START with 'SHELF DECISION:' (publish
        # cleanup, filter, chip glyph); the full reason follows on the proposal
        reason = (_shelf_decision_note(c) + ' · ' + reason)[:500]
      if c.get('md_qty'):
        # v1.5: a day-sized share - the note must START with 'SPLIT n of N ·
        # day d of about k' (prior-share query, drift rule, Refresh); it
        # names the stock clause itself when the shelf also cut the line
        reason = (_md_note(c, order in ship_complete) + ' · ' + reason)[:500]
      elif c.get('md_done'):
        reason = (_md_done_note(c) + ' · ' + reason)[:500]
      elif c.get('stock_qty'):
        # v1.3: the board note must START with 'SPLIT n of N' (drift rule,
        # Refresh); the rest of the shelf is on another order of this plan
        reason = (f"SPLIT {c['stock_qty']:g} of {c['stock_full_qty']:g} - stock: the rest is "
                  f"planned on order {c.get('stock_taker') or '?'} · " + reason)[:500]
      if not dry_run:
        cursor.execute("""
            INSERT INTO dbo.pc_agent_proposal_line
                (proposal_id, nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, rank_no,
                 proposed_tier, load_inches, priority_score, gate_codes, flag_codes, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (proposal_id, order, rel, item, sku, rank, tier,
              float(c.get('load_inches') or 0), int(c.get('priority_score') or 0),
              c.get('gate_codes'), c.get('flag_codes'), reason))
      rows = rows_by_key.get(key) or []
      agg_p = _aggregate(rows) if rows else None
      if agg_p and c.get('stock_qty'):
        agg_p = _scale_agg(agg_p, c['stock_qty'])        # v1.3 partial
      if agg_p and c.get('shelf_decision'):              # v1.4: on the board, not in the load
        agg_p = dict(agg_p, inches=0.0, minutes=0.0, surcharge=0.0)
      if agg_p and c.get('md_qty'):                      # v1.5: today's share of the line
        agg_p = _scale_agg(agg_p, c['md_qty'])
      if agg_p and c.get('md_done'):                     # v1.5: shares cover it - visible at 0 in
        agg_p = dict(agg_p, inches=0.0, minutes=0.0, surcharge=0.0)
      if not dry_run and rows and _upsert_proposed_line(cursor, plan_date, order, rel, item, rows,
                                                        agg_p, tier, reason, actor):
        n_added += 1
        _log_event(cursor, plan_date, order, sku, 'Proposed',
                   new_value=tier, actor=actor, note=reason, rel=rel, item=item)
      out_lines.append({'rank': rank, 'order': order, 'sku': sku,
                        'rel': rel, 'item': item, 'line': _line_label(order, rel, item),
                        'desc': c.get('sku_desc'), 'shipto': c.get('shipto_code'),
                        'tier': tier, 'load_inches': float(c.get('load_inches') or 0),
                        'ship_by': c.get('ship_by_date'),
                        'score': int(c.get('priority_score') or 0),
                        'flags': c.get('flag_codes'), 'reason': reason,
                        'qty_planned': (c.get('md_qty') or c.get('stock_qty') or None),     # v1.3 / v1.5
                        'qty_full': (c.get('md_full') if c.get('md_qty') else c.get('stock_full_qty')) or None,
                        'multiday': (f"day {int(c['md_day'])} of about {int(c['md_about'])}"
                                     if c.get('multiday') else None),     # v1.5
                        'multiday_built_est': bool(c.get('md_done')),
                        'shelf_decision': bool(c.get('shelf_decision')),  # v1.4
                        'decision_taker': c.get('decision_taker'),
                        'decision_load': float(c.get('decision_load') or 0)})

    _cap = None if dry_run else 25      # the compare needs every gated/short line
    gated_out = [{'order': g['nx_order_numb'], 'sku': g['nx_sku'],
                  'item': g['nx_item_no'], 'line': _line_label(g['nx_order_numb'], g['nx_rel_numb'], g['nx_item_no']),
                  'gates': g.get('gate_codes'), 'score': int(g.get('priority_score') or 0),
                  'ship_by': g.get('ship_by_date'),
                  'notes': [n['note'] for n in notes.get((g['nx_order_numb'], g['nx_rel_numb'], g['nx_item_no']), [])
                            if (n['flag_code'] or '')[:1] == 'X']}
                 for g in gated[:_cap]]

    oversize_out = [{'order': o['nx_order_numb'], 'sku': o['nx_sku'],
                     'item': o['nx_item_no'], 'line': _line_label(o['nx_order_numb'], o['nx_rel_numb'], o['nx_item_no']),
                     'load_inches': float(o.get('load_inches') or 0),
                     'group_inches': float(o.get('group_inches') or 0),
                     'qty_open': float(o.get('qty_open') or 0),
                     'score': int(o.get('priority_score') or 0),
                     'ship_by': o.get('ship_by_date'), 'flags': o.get('flag_codes'),
                     'why': o.get('split_reason')}
                    for o in oversize[:15]]
    short_out = [{'order': s['nx_order_numb'], 'sku': s['nx_sku'],
                  'item': s['nx_item_no'], 'line': _line_label(s['nx_order_numb'], s['nx_rel_numb'], s['nx_item_no']),
                  'load_inches': float(s.get('load_inches') or 0),
                  'gates': s.get('gate_codes'), 'score': int(s.get('priority_score') or 0),
                  'ship_by': s.get('ship_by_date'), 'note': s.get('short_note'),
                  'short_of': s.get('short_of')}
                 for s in short_lines[:_cap]]
    blocked_out = [{'order': o['nx_order_numb'], 'sku': o['nx_sku'],
                    'item': o['nx_item_no'], 'line': _line_label(o['nx_order_numb'], o['nx_rel_numb'], o['nx_item_no']),
                    'load_inches': float(o.get('load_inches') or 0),
                    'group_inches': float(o.get('group_inches') or 0),
                    'score': int(o.get('priority_score') or 0),
                    'ship_by': o.get('ship_by_date'), 'why': o.get('block_reason')}
                   for o in blocked[:15]]
    snapshot_warning = None
    if dry_run:
      conn.rollback()           # belt and braces: nothing was written
    else:
      try:
        _snapshot_flags(cursor, plan_date, -1, weight_set_id)
      except Exception as e:      # history must never block the proposal
        snapshot_warning = str(e)
      conn.commit()
    return {'status': 'success', 'proposal_id': proposal_id, 'dry_run': bool(dry_run),
            'plan_date': plan_date.isoformat(),
            'capacity': cap, 'capacity_inches': round(cap_in, 1),
            'fill_pct': ap['fill_pct'], 'target_inches': round(target, 1),
            'already_planned_inches': round(already_in, 1),
            'proposed_inches': round(used, 1),
            'n_proposed': len(picks), 'n_stretch': len(stretch),
            'stretch_inches': round(stretch_in, 1),                    # v1.4.1
            'n_added_to_plan': n_added, 'n_cleared': n_cleared,
            'n_gated': len(gated), 'n_skipped': skipped,
            'n_oversize': len(oversize),
            # v1.5: with the pool on, 'oversize' holds multi-day orders that
            # WAIT for the pool (why names the order that has it) - not
            # split decisions for PC. n_split_needed counts only the latter.
            'n_split_needed': (0 if md_pool > 0 else len(oversize)),
            'n_multiday': len(md_out), 'multiday': md_out,
            'n_multiday_waiting': len(md_waiting), 'multiday_waiting': md_waiting,
            'multiday_pool_inches': round(md_pool, 1), 'multiday_used_inches': round(md_used, 1),
            'multiday_share_pct': ap['multiday_share_pct'],
            'max_line_inches': round(max_line, 1), 'slack_inches': round(slack, 1),
            'n_zero_touch_skipped': skipped_zero,
            'n_candidates': len(cands), 'horizon_end': end.isoformat(),
            'rule': 'WHOLE_ORDER', 'n_groups': n_groups,
            'n_stretch_groups': n_stretch_groups,
            'n_blocked': len(blocked), 'blocked': blocked_out,
            'n_short': len(short_lines), 'short': short_out,
            'n_stock_short': sum(1 for s in short_lines if s.get('stock_short')),      # v1.3
            'n_stock_partial': sum(1 for c, _ in picks + stretch if c.get('stock_qty')),
            'n_shelf_decision': sum(1 for c, _ in picks + stretch if c.get('shelf_decision')),  # v1.4
            'lines': out_lines, 'gated': gated_out, 'oversize': oversize_out,
            'snapshot_warning': snapshot_warning}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Agent propose failed: {e}'}
  finally:
    conn.close()


def _agent_key_ok():
  """Shared secret AGENT_API_KEY (Anvil Secrets) in the X-Agent-Key header."""
  import anvil.secrets
  key = (anvil.server.request.headers or {}).get('x-agent-key')
  try:
    expected = anvil.secrets.get_secret('AGENT_API_KEY')
  except Exception:
    expected = None
  return bool(expected) and key == expected


@anvil.server.http_endpoint('/agent/propose', methods=['POST'])
def agent_propose_endpoint(**kwargs):
  """titan-ai-service calls this at AGENT_RUN_TIME. Launches the
  background run and returns its task_id; poll /agent/status."""
  if not _agent_key_ok():
    return anvil.server.HttpResponse(401, 'unauthorized')
  body = anvil.server.request.body_json or {}
  return ship_plan_agent_propose(body.get('plan_date'), bool(body.get('force')))


def _jsonable(v):
  """Deep make_serializable — HTTP endpoints return JSON, and the task
  result carries dates (ship_by) and Decimals."""
  if isinstance(v, dict):
    return {k: _jsonable(x) for k, x in v.items()}
  if isinstance(v, (list, tuple)):
    return [_jsonable(x) for x in v]
  return make_serializable(v)


@anvil.server.http_endpoint('/agent/status', methods=['GET'])
def agent_status_endpoint(task_id=None, **kwargs):
  if not _agent_key_ok():
    return anvil.server.HttpResponse(401, 'unauthorized')
  return _jsonable(ship_plan_agent_task_status(task_id))


# =============================================================================
# RUSH LANE (Dave, 2026-09-24; migration 091)
#
# A RUSH is any line with an expedited ship method (A05) or PC's manual
# rush flag (A04), ship-by on or before the next business day, not gated
# and not already on that day's plan - whenever it appears, with or
# without work-inches (610908-001-10, a zero-touch UPS 2DAY check valve,
# sat at score 150 all day and never made the plan). Ship-by alone is
# NOT a rush: e21 stamps ship-by = entry date on plain stock orders.
#
# titan-ai-service polls /agent/rush every RUSH_POLL_MINUTES on weekdays.
# Each new rush line is PROPOSED (Must, reason RUSH) - one tap to confirm,
# so the gatekeeper rule and the capacity bar still apply - recorded in
# pc_plan_rush (the dedupe), and handed back grouped by order for ONE
# text per order. After RUSH_CUTOFF_TIME (DB clock; UPS picks up 17:00)
# the line is proposed for the NEXT business day and the text says so.
# =============================================================================

def _rush_params(cursor):
  p = _get_params(cursor)
  raw = (p.get('RUSH_CUTOFF_TIME') or '16:30').strip()
  try:
    hh, mm = raw.split(':')[:2]
    cutoff = (int(hh), int(mm))
  except (ValueError, AttributeError):
    cutoff = (16, 30)
  return {'enabled': str(p.get('RUSH_ENABLED', '1')).strip() == '1',
          'cutoff': cutoff}


def _rush_scan_impl(force=False):
  """One poll: propose every new rush line for the target day, record it,
  return the ones not yet announced (grouped by order) for the service to
  text. Idempotent - a line already in pc_plan_rush for that plan_date is
  skipped, whatever its plan status. Uses the DB clock (Eastern) for the
  cutoff so the Anvil server's UTC clock never decides the day."""
  actor = _AGENT_ACTOR
  conn = get_connection()
  try:
    cursor = conn.cursor()
    rp = _rush_params(cursor)
    if not rp['enabled'] and not force:
      return {'status': 'skipped', 'message': 'RUSH_ENABLED is 0.'}
    cursor.execute("SELECT GETDATE() AS now, CAST(GETDATE() AS DATE) AS today")
    clk = cursor.fetchone()
    now, today = clk['now'], clk['today']
    if isinstance(today, datetime):
      today = today.date()
    after_cutoff = (now.hour, now.minute) >= rp['cutoff']
    plan_date = today
    if today.weekday() >= 5 or after_cutoff:
      plan_date = _next_business_day(today + timedelta(days=1))

    cursor.execute("SELECT * FROM dbo.vw_pc_rush_candidates")
    cands = [serialize_row(r) for r in cursor.fetchall()]
    if not cands:
      return {'status': 'success', 'plan_date': plan_date.isoformat(),
              'after_cutoff': after_cutoff, 'n_candidates': 0, 'n_proposed': 0,
              'orders': []}

    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, line_status
        FROM dbo.pc_ship_plan_line WHERE plan_date = %s
    """, (plan_date,))
    on_plan = {(r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']): r['line_status']
               for r in cursor.fetchall()}
    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no FROM dbo.pc_plan_rush
        WHERE plan_date = %s
    """, (plan_date,))
    seen = {(r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']) for r in cursor.fetchall()}
    # a line already on the plan TODAY (e.g. rush landed at 16:35 after PC
    # planned it by hand for today) is not re-proposed for tomorrow
    today_plan = set()
    if plan_date != today:
      cursor.execute("""
          SELECT nx_order_numb, nx_rel_numb, nx_item_no FROM dbo.pc_ship_plan_line
          WHERE plan_date = %s AND line_status IN %s
      """, (today, _COUNTS_AGAINST_CAP))
      today_plan = {(r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']) for r in cursor.fetchall()}

    header_ok = False
    new_rows, n_proposed = [], 0
    for c in cands:
      key = (c['nx_order_numb'], c['nx_rel_numb'], c['nx_item_no'])
      if key in seen or key in today_plan:
        continue
      st = on_plan.get(key)
      proposed = False
      if st in _ACTIVE_STATUSES:
        note = f'already on the plan ({st})'
      else:
        if not header_ok:
          _ensure_header(cursor, plan_date, actor)
          header_ok = True
        rows = _fetch_candidate_rows(cursor, c['nx_order_numb'], c['nx_rel_numb'], c['nx_item_no'])
        if not rows:
          note = 'no open quantity in the feed'
        else:
          due = str(c.get('ship_by_date') or '')[:10]
          reason = (f"Must: RUSH - {c.get('nx_mos') or c.get('rush_basis')}"
                    f"{' (PC rush flag)' if c.get('rush_basis') == 'A04' else ''}, due {due}"
                    f"{' - after cutoff, proposed for ' + plan_date.isoformat() if after_cutoff or plan_date != today else ''}"
                    f" — {float(c.get('load_inches') or 0):,.1f} in, score {int(c.get('priority_score') or 0)}")[:200]
          if _upsert_proposed_line(cursor, plan_date, c['nx_order_numb'], c['nx_rel_numb'],
                                   c['nx_item_no'], rows, _aggregate(rows), 'Must', reason, actor):
            cursor.execute("""
                UPDATE dbo.pc_ship_plan_line SET priority_reason = 'RUSH'
                WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
            """, (plan_date, c['nx_order_numb'], c['nx_rel_numb'], c['nx_item_no']))
            _log_event(cursor, plan_date, c['nx_order_numb'], c['nx_sku'], 'Proposed',
                       new_value='Must', actor=actor, note=reason,
                       rel=c['nx_rel_numb'], item=c['nx_item_no'])
            proposed = True
            n_proposed += 1
            note = reason
          else:
            note = 'already on the plan'
      cursor.execute("""
          INSERT INTO dbo.pc_plan_rush
              (plan_date, nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, nx_mos,
               load_inches, priority_score, after_cutoff, proposed, announced_at, note)
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, GETDATE(), %s)
      """, (plan_date, c['nx_order_numb'], c['nx_rel_numb'], c['nx_item_no'], c['nx_sku'],
            c.get('nx_mos'), float(c.get('load_inches') or 0), int(c.get('priority_score') or 0),
            1 if (after_cutoff or plan_date != today) else 0, 1 if proposed else 0, note[:300]))
      c['proposed'] = proposed
      c['plan_note'] = note
      new_rows.append(c)

    if n_proposed:
      _bump_revision(cursor, plan_date, actor)
    conn.commit()

    # group by order for the text: one message per sales order
    orders, by_order = [], {}
    for c in new_rows:
      o = by_order.get(c['nx_order_numb'])
      if o is None:
        o = by_order[c['nx_order_numb']] = {
          'order': c['nx_order_numb'],
          'customer': _customer_label(c.get('nx_cust_name'), c.get('nx_cust_city'),
                                      c.get('nx_cust_state'), c.get('shipto_code')),
          'mos': c.get('nx_mos'), 'carrier': c.get('carrier'),
          'ship_by': c.get('ship_by_date'), 'basis': c.get('rush_basis'),
          'lines': [], 'inches': 0.0, 'n_proposed': 0, 'n_already': 0}
        orders.append(o)
      o['lines'].append({'line': _line_label(c['nx_order_numb'], c['nx_rel_numb'], c['nx_item_no']),
                         'sku': c['nx_sku'], 'desc': c.get('sku_desc'),
                         'qty': float(c.get('nx_qty_ordered') or 0),
                         'status': c.get('nx_item_status_desc'),
                         'load_inches': float(c.get('load_inches') or 0),
                         'zero_touch': float(c.get('load_inches') or 0) == 0.0,
                         'proposed': c['proposed'], 'note': c['plan_note']})
      o['inches'] += float(c.get('load_inches') or 0)
      o['n_proposed' if c['proposed'] else 'n_already'] += 1
    return {'status': 'success', 'plan_date': plan_date.isoformat(),
            'today': today.isoformat(), 'after_cutoff': bool(after_cutoff),
            'cutoff': '%02d:%02d' % rp['cutoff'],
            'n_candidates': len(cands), 'n_new': len(new_rows), 'n_proposed': n_proposed,
            'orders': orders}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Rush scan failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_rush_scan(force=False):
  """PC's manual poll (no button yet - callable for the console / a
  future 'Check for rushes' link)."""
  return _rush_scan_impl(force=force)


@anvil.server.http_endpoint('/agent/rush', methods=['POST'])
def agent_rush_endpoint(**kwargs):
  """titan-ai-service polls this every RUSH_POLL_MINUTES. Synchronous -
  the scan is a handful of queries, well inside the 30 s limit."""
  if not _agent_key_ok():
    return anvil.server.HttpResponse(401, 'unauthorized')
  body = anvil.server.request.body_json or {}
  return _jsonable(_rush_scan_impl(force=bool(body.get('force'))))


@anvil.server.callable
def ship_plan_confirm_line(plan_date, order, rel, item):
  """PC accepts one Proposed line: it becomes Planned, tier unchanged."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND line_status = 'Proposed'
    """, (plan_date, order, rel, item))
    hit = cursor.fetchone()
    if not hit:
      return {'status': 'error', 'message': 'Line is not Proposed.'}
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Planned', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND line_status = 'Proposed'
    """, (actor, plan_date, order, rel, item))
    _log_event(cursor, plan_date, order, hit['nx_sku'], 'Confirmed', actor=actor,
               rel=rel, item=item)
    _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Confirm failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def ship_plan_confirm_all_proposed(plan_date):
  """PC accepts every Proposed line on the day in one go."""
  plan_date = _parse_date(plan_date)
  actor = _current_actor()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND line_status = 'Proposed'
    """, (plan_date,))
    todo = cursor.fetchall()
    for r in todo:
      cursor.execute("""
          UPDATE dbo.pc_ship_plan_line
          SET line_status = 'Planned', updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
            AND line_status = 'Proposed'
      """, (actor, plan_date, r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']))
      _log_event(cursor, plan_date, r['nx_order_numb'], r['nx_sku'],
                 'Confirmed', actor=actor, note='accept all',
                 rel=r['nx_rel_numb'], item=r['nx_item_no'])
    if todo:
      _bump_revision(cursor, plan_date, actor)
    conn.commit()
    return {'status': 'success', 'confirmed': len(todo)}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Confirm-all failed: {e}'}
  finally:
    conn.close()


def _settle_proposal_outcomes(cursor, plan_date, actor):
  """At publish: auto-confirm any line still Proposed (publishing IS the
  acceptance), then score the latest proposal line by line —
  KEPT / RETIERED / DROPPED, plus ADDED_BY_PC for plan lines the agent
  did not propose. Proposal status: ACCEPTED (all kept, nothing added),
  MODIFIED, or REJECTED (nothing kept). This is the hit-rate record."""
  cursor.execute("""
      SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND line_status = 'Proposed'
  """, (plan_date,))
  for r in cursor.fetchall():
    cursor.execute("""
        UPDATE dbo.pc_ship_plan_line
        SET line_status = 'Planned', updated_by = %s, updated_at = GETDATE()
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (actor, plan_date, r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']))
    _log_event(cursor, plan_date, r['nx_order_numb'], r['nx_sku'],
               'Confirmed', actor=actor, note='auto-confirmed at publish',
               rel=r['nx_rel_numb'], item=r['nx_item_no'])
  cursor.execute("""
      SELECT TOP 1 proposal_id FROM dbo.pc_agent_proposal
      WHERE plan_date = %s AND status = 'PROPOSED'
      ORDER BY generated_at DESC
  """, (plan_date,))
  prop = cursor.fetchone()
  if not prop:
    return None
  pid = prop['proposal_id']
  cursor.execute("""
      SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, tier, line_status
      FROM dbo.pc_ship_plan_line WHERE plan_date = %s
  """, (plan_date,))
  plan = {(r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no']): r for r in cursor.fetchall()}
  cursor.execute("""
      SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, proposed_tier
      FROM dbo.pc_agent_proposal_line WHERE proposal_id = %s
  """, (pid,))
  kept = retiered = dropped = 0
  proposed_keys = set()
  for r in cursor.fetchall():
    key = (r['nx_order_numb'], r['nx_rel_numb'], r['nx_item_no'])
    proposed_keys.add(key)
    pl = plan.get(key)
    if pl and pl['line_status'] in _COUNTS_AGAINST_CAP:
      outcome = 'KEPT' if pl['tier'] == r['proposed_tier'] else 'RETIERED'
      kept += outcome == 'KEPT'
      retiered += outcome == 'RETIERED'
    else:
      outcome, dropped = 'DROPPED', dropped + 1
    cursor.execute("""
        UPDATE dbo.pc_agent_proposal_line SET outcome = %s
        WHERE proposal_id = %s AND nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
    """, (outcome, pid, key[0], key[1], key[2]))
  added = 0
  for key, pl in plan.items():
    if key in proposed_keys or pl['line_status'] not in _COUNTS_AGAINST_CAP:
      continue
    added += 1
    cursor.execute("""
        INSERT INTO dbo.pc_agent_proposal_line
            (proposal_id, nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, rank_no,
             proposed_tier, reason, outcome)
        VALUES (%s, %s, %s, %s, %s, 0, %s, 'not proposed by the agent', 'ADDED_BY_PC')
    """, (pid, key[0], key[1], key[2], pl['nx_sku'], pl['tier'] or 'Next'))
  if kept + retiered == 0:
    status = 'REJECTED'
  elif dropped == 0 and retiered == 0 and added == 0:
    status = 'ACCEPTED'
  else:
    status = 'MODIFIED'
  cursor.execute("""
      UPDATE dbo.pc_agent_proposal
      SET status = %s, reviewed_by = %s, reviewed_at = GETDATE()
      WHERE proposal_id = %s
  """, (status, actor, pid))
  return {'proposal_id': pid, 'status': status, 'kept': kept,
          'retiered': retiered, 'dropped': dropped, 'added_by_pc': added}


@anvil.server.callable
def ship_plan_get_proposal(plan_date):
  """Latest proposal for the day (any status) — header + lines, for the
  'Agent proposal' popup and the retro."""
  plan_date = _parse_date(plan_date)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP 1 * FROM dbo.pc_agent_proposal
        WHERE plan_date = %s ORDER BY generated_at DESC
    """, (plan_date,))
    hdr = cursor.fetchone()
    if not hdr:
      return {'status': 'success', 'proposal': None, 'lines': []}
    cursor.execute("""
        SELECT * FROM dbo.pc_agent_proposal_line
        WHERE proposal_id = %s ORDER BY rank_no
    """, (hdr['proposal_id'],))
    return {'status': 'success', 'proposal': serialize_row(hdr),
            'lines': [serialize_row(r) for r in cursor.fetchall()]}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()