import anvil.server
from datetime import date, datetime, timedelta

from .daily_ship_plan import (get_connection, _parse_date, serialize_row,
make_serializable, _line_label)

# =============================================================================
# COMPLETED WORK ORDERS BY DAY  (wo-1.0.1, 2026-09-16)
#
# Server module name: daily_plan_completed. It was first shipped as
# 'wo_completed', the same name as the client FORM package; Anvil puts
# client packages and server modules in one import namespace, so the
# downlink resolved 'wo_completed' to the form (RuntimeError: Cannot
# import Form templates on the server). Server modules here follow
# daily_ship_plan / daily_plan_retro - never a form's name.
#
# Dave, before the 09:00 PC/Sales meeting: "an additional form that displays
# which work orders were completed by day ... a running total of the
# work-inches so we can easily compare actual vs. proposed."
#
# Facts this is built on (checked against Nexus 2026-09-16):
#   * Until the tablets are at every bench, most work orders never get a
#     floor Complete event. They reach 'Shipped - Verify' (or 'Invoiced -
#     Verify' / 'Closed - Verify') when the e21 import sees the sales-order
#     line shipped - recorded in work_order_changes as SO_Shipped /
#     SO_Invoiced with detected_at (every ~15 min until ~18:00). Rows that
#     ARE completed on the floor carry a work_order_events 'Complete' row and
#     completed_at. 2026-09-15: 163 done, 5 from the floor, 158 from e21.
#   * DAY BASIS (Dave's call): the EARLIEST of floor completion and e21
#     shipped detection - the day the work was done, not the day the paper
#     caught up.
#   * WORK-INCHES: the publish-time flag snapshot (pc_plan_flag_snapshot,
#     every open e21 line at publish, planned or not - the same figure the
#     retro uses), falling back to the day's plan line. A line born after
#     publish and shipped the same day has no estimate; it is shown with '-'
#     and counted separately, never invented.
#   * COMPARISON (Dave's call): actual vs the PUBLISHED plan - what PC
#     committed - not the agent's proposal.
#   * ZERO-TOUCH (Dave's call): shown with a Z marker, excluded from the
#     running total.
# Nothing here writes. No migration - it reads tables that already exist.
# =============================================================================

_DONE_STATUSES = ('Complete', 'Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify')
_ACTIVE_PLAN = ('Planned', 'Confirmed', 'Proposed')

_ROWS_SQL = """
SELECT wo.work_order_id, wo.work_order_number, wo.nx_order_numb, wo.nx_rel_numb, wo.nx_item_no,
       wo.nx_sku, wo.sku_desc, wo.qty, wo.status, wo.completed_by, wo.assigned_assembler,
       wo.completed_at, wo.floor_complete_at, wo.shipped_detected_at, wo.done_at,
       snap.load_inches AS snap_load, snap.was_planned, snap.flag_codes, snap.tier AS snap_tier,
       pl.load_inches AS plan_load, pl.line_status, pl.tier AS plan_tier
FROM (
  SELECT w.work_order_id, w.work_order_number,
         w.sales_order_number AS nx_order_numb, w.sales_order_rel AS nx_rel_numb,
         LTRIM(RTRIM(CAST(w.sales_order_line AS VARCHAR(10)))) AS nx_item_no,
         w.part_number AS nx_sku, w.sku_desc,
         COALESCE(w.wo_override_qty, w.quantity) AS qty, w.status,
         w.completed_by, w.assigned_assembler, w.completed_at,
         fc.event_at AS floor_complete_at, sh.detected_at AS shipped_detected_at, d.done_at
  FROM dbo.work_orders w
  OUTER APPLY (SELECT MIN(e.event_at) AS event_at FROM dbo.work_order_events e
               WHERE e.work_order_id = w.work_order_id AND e.event_type = 'Complete') fc
  OUTER APPLY (SELECT MIN(c.detected_at) AS detected_at FROM dbo.work_order_changes c
               WHERE c.work_order_id = w.work_order_id
                 AND c.change_type IN ('SO_Shipped', 'SO_Invoiced')) sh
  CROSS APPLY (SELECT MIN(t) AS done_at
               FROM (VALUES (w.completed_at), (fc.event_at), (sh.detected_at)) v(t)
               WHERE t IS NOT NULL) d
  WHERE w.status IN ('Complete', 'Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify')
) wo
LEFT JOIN dbo.pc_plan_flag_snapshot snap
       ON snap.plan_date = %s
      AND snap.revision_no = (SELECT MAX(x.revision_no) FROM dbo.pc_plan_flag_snapshot x
                              WHERE x.plan_date = %s)
      AND snap.nx_order_numb = wo.nx_order_numb
      AND snap.nx_rel_numb   = wo.nx_rel_numb
      AND snap.nx_item_no    = wo.nx_item_no
LEFT JOIN dbo.pc_ship_plan_line pl
       ON pl.plan_date = %s
      AND pl.nx_order_numb = wo.nx_order_numb
      AND pl.nx_rel_numb   = wo.nx_rel_numb
      AND pl.nx_item_no    = wo.nx_item_no
WHERE wo.done_at >= %s AND wo.done_at < %s
ORDER BY wo.done_at, wo.nx_order_numb, wo.nx_rel_numb, wo.nx_item_no
"""


def _plan_state(r):
  """What the plan said about this line on that day."""
  st = r.get('line_status')
  if st in _ACTIVE_PLAN:
    return 'Stretch' if (r.get('plan_tier') == 'Stretch') else 'Planned'
  if st:                                  # Removed / Unplaced / Snoozed
    return st
  if r.get('was_planned'):
    return 'Planned'
  return 'Unplanned'


@anvil.server.callable
def wo_completed_get_day(day):
  """Everything the form needs for ONE day in one round trip: the day's
  published-plan header, the completed work orders in the order they were
  done with a running total, and the totals."""
  day = _parse_date(day)
  nxt = day + timedelta(days=1)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT plan_status, published_at, revision_no, capacity_inches
        FROM dbo.pc_ship_plan WHERE plan_date = %s
    """, (day,))
    hdr = cursor.fetchone()
    cursor.execute("""
        SELECT SUM(CASE WHEN tier <> 'Stretch' THEN load_inches ELSE 0 END) AS planned_inches,
               SUM(CASE WHEN tier = 'Stretch'  THEN load_inches ELSE 0 END) AS stretch_inches,
               COUNT(*) AS n_lines
        FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND line_status IN ('Planned', 'Confirmed', 'Proposed')
    """, (day,))
    p = cursor.fetchone() or {}
    cursor.execute(_ROWS_SQL, (day, day, day, day, nxt))
    raw = [serialize_row(r) for r in cursor.fetchall()]
  except Exception as e:
    return {'status': 'error', 'message': f'Completed load error: {e}'}
  finally:
    conn.close()

  rows, running = [], 0.0
  tot = {'n': 0, 'n_floor': 0, 'n_e21': 0, 'n_zero': 0, 'n_no_estimate': 0,
         'actual_inches': 0.0, 'actual_planned_inches': 0.0, 'actual_unplanned_inches': 0.0,
         'n_planned_done': 0}
  for r in raw:
    load = r.get('snap_load')
    if load is None:
      load = r.get('plan_load')
    codes = ',' + (r.get('flag_codes') or '') + ','
    zero = (',C03,' in codes) or (load is not None and float(load) == 0.0
                                  and r.get('flag_codes') is not None)
    state = _plan_state(r)
    on_plan = state in ('Planned', 'Stretch')
    basis = ('floor' if r.get('floor_complete_at') else
             'e21' if r.get('shipped_detected_at') else 'completed_at')
    tot['n'] += 1
    tot['n_floor' if basis == 'floor' else 'n_e21'] += 1
    if on_plan:
      tot['n_planned_done'] += 1
    if zero:
      tot['n_zero'] += 1
    elif load is None:
      tot['n_no_estimate'] += 1
    else:
      running += float(load)
      tot['actual_inches'] += float(load)
      tot['actual_planned_inches' if on_plan else 'actual_unplanned_inches'] += float(load)
    r.update({
      'line_label': _line_label(r['nx_order_numb'], r.get('nx_rel_numb'), r.get('nx_item_no')),
      'load_inches': (None if load is None else round(float(load), 1)),
      'zero_touch': bool(zero),
      'plan_state': state,
      'on_plan': on_plan,
      'basis': basis,
      'running_inches': round(running, 1),
    })
    rows.append(r)

  planned = float(p.get('planned_inches') or 0)
  tot = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in tot.items()}
  return {'status': 'success',
          'day': make_serializable(day),
          'header': serialize_row(hdr) if hdr else None,
          'planned_inches': round(planned, 1),
          'stretch_inches': round(float(p.get('stretch_inches') or 0), 1),
          'n_plan_lines': int(p.get('n_lines') or 0),
          'pct_of_plan': (round(tot['actual_inches'] / planned * 100.0, 1) if planned else None),
          'totals': tot,
          'rows': rows}