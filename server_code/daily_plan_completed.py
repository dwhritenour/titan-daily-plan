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
WITH first_complete AS (
    SELECT e.work_order_id, MIN(e.event_at) AS first_at
    FROM   dbo.work_order_events e
    WHERE  e.event_type = 'Complete'
    GROUP  BY e.work_order_id
),
wo AS (
    SELECT w.work_order_id, w.work_order_number,
           LTRIM(RTRIM(w.sales_order_number))                    AS nx_order_numb,
           LTRIM(RTRIM(w.sales_order_rel))                       AS nx_rel_numb,
           LTRIM(RTRIM(CAST(w.sales_order_line AS VARCHAR(10)))) AS nx_item_no,
           w.part_number AS nx_sku, w.sku_desc,
           COALESCE(w.wo_override_qty, w.quantity)               AS wo_qty,
           w.status, w.completed_by, w.assigned_assembler, w.completed_at,
           fc.first_at AS floor_complete_at,
           CAST(fc.first_at AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS datetime) AS done_at
    FROM   first_complete fc
    JOIN   dbo.work_orders w ON w.work_order_id = fc.work_order_id
    WHERE  CAST(fc.first_at AT TIME ZONE 'UTC' AT TIME ZONE 'Eastern Standard Time' AS date) = %s
)
SELECT wo.work_order_id, wo.work_order_number, wo.nx_order_numb, wo.nx_rel_numb, wo.nx_item_no,
       wo.nx_sku, wo.sku_desc,
       COALESCE(so.nx_qty_ordered, snap.qty_open, pl.qty_planned, wo.wo_qty) AS qty,
       COALESCE(so.nx_qty_ordered, snap.qty_open, pl.qty_planned)            AS qty_ordered,
       wo.wo_qty, so.part_type,
       wo.status, wo.completed_by, wo.assigned_assembler,
       wo.completed_at, wo.floor_complete_at, CAST(NULL AS datetime) AS shipped_detected_at, wo.done_at,
       snap.load_inches AS snap_load, snap.was_planned, snap.flag_codes, snap.tier AS snap_tier,
       CASE WHEN snap2.load_inches IS NULL THEN NULL
            WHEN ISNULL(snap2.qty_open, 0) > 0
            THEN snap2.load_inches * COALESCE(so.nx_qty_ordered, wo.wo_qty) / snap2.qty_open
            ELSE snap2.load_inches END                                       AS snap2_load,
       snap2.was_planned AS was_planned2, snap2.flag_codes AS flag_codes2,
       pl.load_inches AS plan_load, pl.line_status, pl.tier AS plan_tier
FROM wo
OUTER APPLY (
    SELECT TOP 1 u.nx_qty_ordered, u.part_type
    FROM (
        SELECT 1 AS src, ISNULL(s.nx_qty_ordered, s.nx_qty_balance_due) AS nx_qty_ordered,
               RTRIM(s.nx_part_type) AS part_type
        FROM dbo.sales_orders_due s
        WHERE LTRIM(RTRIM(s.nx_order_numb)) = wo.nx_order_numb
          AND LTRIM(RTRIM(s.nx_rel_numb))   = wo.nx_rel_numb
          AND LTRIM(RTRIM(CAST(s.nx_item_no AS VARCHAR(10)))) = wo.nx_item_no
        UNION ALL
        SELECT 2 AS src, s2.nx_qty_ordered, RTRIM(s2.nx_part_type) AS part_type
        FROM dbo.sales_orders_ship s2
        WHERE LTRIM(RTRIM(s2.nx_order_numb)) = wo.nx_order_numb
          AND LTRIM(RTRIM(s2.nx_rel_numb))   = wo.nx_rel_numb
          AND LTRIM(RTRIM(CAST(s2.nx_item_no AS VARCHAR(10)))) = wo.nx_item_no
    ) u
    ORDER BY u.src
) so
LEFT JOIN dbo.pc_plan_flag_snapshot snap
       ON snap.plan_date = %s
      AND snap.revision_no = (SELECT MAX(x.revision_no) FROM dbo.pc_plan_flag_snapshot x
                              WHERE x.plan_date = %s)
      AND LTRIM(RTRIM(snap.nx_order_numb)) = wo.nx_order_numb
      AND LTRIM(RTRIM(snap.nx_rel_numb))   = wo.nx_rel_numb
      AND LTRIM(RTRIM(snap.nx_item_no))    = wo.nx_item_no
LEFT JOIN dbo.pc_plan_flag_snapshot snap2
       ON snap.nx_order_numb IS NULL
      AND snap2.plan_date = %s
      AND snap2.revision_no = (SELECT MAX(x.revision_no) FROM dbo.pc_plan_flag_snapshot x
                               WHERE x.plan_date = %s)
      AND LTRIM(RTRIM(snap2.nx_order_numb)) = wo.nx_order_numb
      AND snap2.nx_sku = wo.nx_sku
      AND ISNULL(snap2.nx_rel_numb, '') = ''
LEFT JOIN dbo.pc_ship_plan_line pl
       ON pl.plan_date = %s
      AND LTRIM(RTRIM(pl.nx_order_numb)) = wo.nx_order_numb
      AND LTRIM(RTRIM(pl.nx_rel_numb))   = wo.nx_rel_numb
      AND LTRIM(RTRIM(pl.nx_item_no))    = wo.nx_item_no
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
    cursor.execute(_ROWS_SQL, (day, day, day, day, day, day))
    raw = [serialize_row(r) for r in cursor.fetchall()]
  except Exception as e:
    return {'status': 'error', 'message': f'Completed load error: {e}'}
  finally:
    conn.close()

  rows, running = [], 0.0
  tot = {'n': 0, 'n_floor': 0, 'n_e21': 0, 'n_zero': 0, 'n_no_estimate': 0,
         'n_done_floor': 0, 'n_done_no_estimate': 0,
         'actual_inches': 0.0, 'actual_planned_inches': 0.0, 'actual_unplanned_inches': 0.0,
         'n_planned_done': 0}
  for r in raw:
    load = r.get('snap_load')
    was_planned = r.get('was_planned')
    codes = r.get('flag_codes')
    if load is None and r.get('snap2_load') is not None:
      load, was_planned, codes = r['snap2_load'], r.get('was_planned2'), r.get('flag_codes2')
    if load is None:
      load = r.get('plan_load')
    ctxt = ',' + (codes or '') + ','
    zero = (',C03,' in ctxt) or (load is not None and float(load) == 0.0 and codes is not None) \
    or ((r.get('part_type') or '') in ('CV', 'BV', 'MS') and load is None)
    r['was_planned'] = was_planned
    state = _plan_state(r)
    on_plan = state in ('Planned', 'Stretch')
    basis = 'floor'
    tot['n'] += 1
    tot['n_floor'] += 1
    tot['n_done_floor'] += 1
    if on_plan:
      tot['n_planned_done'] += 1
    if zero:
      tot['n_zero'] += 1
    elif load is None:
      tot['n_no_estimate'] += 1
      tot['n_done_no_estimate'] += 1
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
          'done_inches': tot['actual_inches'],
          'n_done_floor': tot['n_done_floor'],
          'n_done_no_estimate': tot['n_done_no_estimate'],
          'totals': tot,
          'rows': rows}