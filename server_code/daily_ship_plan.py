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


def get_connection():
  return db_config.get_nexus_connection()


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
      SELECT r.employee_id, o.capacity_factor
      FROM dbo.em_assembler_roster r
      LEFT JOIN dbo.pc_assembler_day_override o
             ON o.employee_id = r.employee_id AND o.override_date = %s
      WHERE r.end_date IS NULL
  """, (plan_date,))
  rows = cursor.fetchall()
  roster_count = len(rows)
  overrides = sum(1 for r in rows if r['capacity_factor'] is not None)
  effective_crew = sum(
    float(r['capacity_factor']) if r['capacity_factor'] is not None else 1.0
    for r in rows)

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
                        f"{overrides} override) x {hours:g}h / {eff:g} - base"),
  }


def _planned_inches(cursor, plan_date):
  cursor.execute("""
      SELECT COALESCE(SUM(load_inches), 0) AS planned
      FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND line_status IN %s
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
                             limit=300):
  """Left pane. The already-planned guard lives in the VIEW."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    where, args = [], []
    if search:
      like = f'%{search.strip()}%'
      where.append("(nx_order_numb LIKE %s OR nx_sku LIKE %s "
                   "OR sku_desc LIKE %s OR shipto_code LIKE %s)")
      args += [like, like, like, like]
    if bucket in ('ST', 'ZERO', 'OTHER'):
      where.append("bucket = %s")
      args.append(bucket)
    if due_within_days:
      where.append("ship_by_date <= %s")
      args.append(date.today() + timedelta(days=int(due_within_days)))
    if special_only:
      where.append("n_special > 0")
    if defaulted_only:
      where.append("n_class_defaulted > 0")
    sql = ("SELECT TOP %d * FROM dbo.vw_pc_ship_candidates"
           % max(1, min(int(limit), 1000)))
    if where:
      sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ship_by_date, load_inches DESC"
    cursor.execute(sql, tuple(args))
    rows = [serialize_row(r) for r in cursor.fetchall()]
    # Feed freshness for the always-on Orders chip — age computed
    # SERVER-side (GETDATE) so browser clocks can't skew it.
    cursor.execute("""
        SELECT MAX(nx_last_updated) AS feed_last_updated,
               DATEDIFF(minute, MAX(nx_last_updated), GETDATE()) AS feed_age_minutes
        FROM dbo.sales_orders_due
    """)
    f = cursor.fetchone() or {}
    return {'status': 'success', 'candidates': rows,
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
        SELECT l.*, live.live_qty
        FROM dbo.pc_ship_plan_line l
        OUTER APPLY (SELECT SUM(s.nx_qty_balance_due) AS live_qty
                     FROM dbo.sales_orders_due s
                     WHERE s.nx_order_numb = l.nx_order_numb
                       AND s.nx_sku = l.nx_sku) live
        WHERE l.plan_date = %s
        ORDER BY CASE l.line_status WHEN 'Unplaced' THEN 0 ELSE 1 END,
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
                        if l['line_status'] in _COUNTS_AGAINST_CAP)
    return {'status': 'success',
            'header': serialize_row(header) if header else None,
            'lines': lines,
            'planned_inches': round(planned_total, 1),
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
                            role='PC', actor=None):
  cursor.execute("""
      INSERT INTO dbo.pc_ship_plan_line
          (plan_date, nx_order_numb, nx_sku, qty_planned, ship_by_date,
           shipto_code, carrier, load_inches, load_minutes,
           surcharge_minutes, line_status, added_by_role, created_by)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Planned', %s, %s)
  """, (plan_date, order, sku, agg['qty'], agg['ship_by'], agg['shipto'],
        agg['carrier'], agg['inches'], agg['minutes'], agg['surcharge'],
        role, actor or _current_actor()))
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
           ('Standard', 'Special') else 'n/a'),
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
def ship_plan_add_line(plan_date, order, sku):
  """Add an (order, SKU) to a day. The line is ALWAYS added — over-cap is
  reported back so the client runs the cap-hit dialog (snooze / unplaced /
  keep). Never a silent drop, never a lost order."""
  plan_date = _parse_date(plan_date)
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
              snooze_to_date = NULL, updated_by = %s, updated_at = GETDATE()
          WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
      """, (agg['qty'], agg['ship_by'], agg['shipto'], agg['carrier'],
            agg['inches'], agg['minutes'], agg['surcharge'], actor,
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
               ('Standard', 'Special') else 'n/a'),
              r['class_was_defaulted'], r['size_inches'],
              r['minutes_charged']))
    else:
      _insert_line_and_source(cursor, plan_date, order, sku, rows, agg,
                              actor=actor)

    _log_event(cursor, plan_date, order, sku, 'Added',
               new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in",
               actor=actor)

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
                            actor=actor)
    _log_event(cursor, to_date, order, sku, 'Added',
               new_value=f"{agg['qty']:g} pcs / {agg['inches']:.1f} in",
               actor=actor, note=f'snoozed from {plan_date}')
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
             ('Standard', 'Special') else 'n/a'),
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