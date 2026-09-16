import anvil.server
from datetime import date, datetime, timedelta

from .daily_ship_plan import (get_connection, _parse_date, serialize_row,
make_serializable, _line_label, _customer_label)

# =============================================================================
# ORDERS SHIPPED BY DAY  (ship-1.0.0, 2026-09-16)
#
# Twin of daily_plan_completed / the wo_completed form, but the SOURCE is
# e21: dbo.sales_orders_ship, Dave's extract of sales-order lines with an
# invoice date. "In e21, the moment a sales order is shipped, an invoice is
# automatically generated" (Dave) - so nx_ddate_invoice IS the ship date.
# The two forms answer different questions and stay separate on purpose:
#   wo_completed  -> work orders the shop worked / closed (floor events +
#                    e21 detection; the tablets will make this the build log)
#   orders_shipped -> what actually left the building, per e21
#
# Facts (checked 2026-09-16): invoice date is a DATE, no time - so the day's
# order comes from the work order's e21-shipped detection time when the line
# has a work order (167 of 168 on 09/15), else the line sorts last with no
# time. Work-inches: publish-time snapshot by item (pre-066 days fall back to
# order + SKU), else the day's plan line, else '-'. Zero-touch shown as Z, not
# totalled. Comparison is against the PUBLISHED plan. Payload keys match the
# wo_completed row template so the same row form renders both grids.
# Nothing here writes. No migration.
# =============================================================================

_ACTIVE_PLAN = ('Planned', 'Confirmed', 'Proposed')

_ROWS_SQL = """
SELECT RTRIM(s.nx_order_numb) AS nx_order_numb, RTRIM(s.nx_rel_numb) AS nx_rel_numb,
       LTRIM(RTRIM(CAST(s.nx_item_no AS VARCHAR(10)))) AS nx_item_no,
       RTRIM(s.nx_sku) AS nx_sku, RTRIM(s.nx_sku_desc) AS sku_desc, RTRIM(s.nx_part_type) AS part_type,
       s.nx_qty_ordered AS qty, s.nx_qty_balance_due AS qty_balance,
       RTRIM(s.nx_item_status_desc) AS status, RTRIM(s.nx_carrier) AS carrier, RTRIM(s.nx_mos) AS mos,
       RTRIM(s.nx_cust_name) AS nx_cust_name, RTRIM(s.nx_cust_city) AS nx_cust_city,
       RTRIM(s.nx_cust_state) AS nx_cust_state, RTRIM(s.nx_shipto_code) AS shipto_code,
       s.nx_ddate_invoice AS invoice_date,
       w.work_order_number, w.status AS wo_status, w.assigned_assembler, w.completed_at,
       fc.event_at AS floor_complete_at, sh.detected_at AS shipped_detected_at,
       snap.load_inches AS snap_load, snap.was_planned, snap.flag_codes,
       -- pre-066 snapshot rows aggregate an order's same-SKU lines: pro-rate by qty
       CASE WHEN snap2.load_inches IS NULL THEN NULL
            WHEN ISNULL(snap2.qty_open, 0) > 0
            THEN snap2.load_inches * s.nx_qty_ordered / snap2.qty_open
            ELSE snap2.load_inches END                  AS snap2_load,
       snap2.was_planned AS was_planned2, snap2.flag_codes AS flag_codes2,
       pl.load_inches AS plan_load, pl.line_status, pl.tier AS plan_tier
FROM dbo.sales_orders_ship s
LEFT JOIN dbo.work_orders w
       ON w.sales_order_number = s.nx_order_numb
      AND w.sales_order_rel    = s.nx_rel_numb
      AND w.sales_order_line   = s.nx_item_no
      AND w.status <> 'Cancelled'
OUTER APPLY (SELECT MIN(e.event_at) AS event_at FROM dbo.work_order_events e
             WHERE e.work_order_id = w.work_order_id AND e.event_type = 'Complete') fc
OUTER APPLY (SELECT MIN(c.detected_at) AS detected_at FROM dbo.work_order_changes c
             WHERE c.work_order_id = w.work_order_id
               AND c.change_type IN ('SO_Shipped', 'SO_Invoiced')) sh
LEFT JOIN dbo.pc_plan_flag_snapshot snap                       -- item grain (066+)
       ON snap.plan_date = %s
      AND snap.revision_no = (SELECT MAX(x.revision_no) FROM dbo.pc_plan_flag_snapshot x
                              WHERE x.plan_date = %s)
      AND snap.nx_order_numb = s.nx_order_numb
      AND snap.nx_rel_numb   = s.nx_rel_numb
      AND snap.nx_item_no    = CAST(s.nx_item_no AS VARCHAR(3))
LEFT JOIN dbo.pc_plan_flag_snapshot snap2                      -- pre-066 rows: order + SKU
       ON snap.nx_order_numb IS NULL
      AND snap2.plan_date = %s
      AND snap2.revision_no = (SELECT MAX(x.revision_no) FROM dbo.pc_plan_flag_snapshot x
                               WHERE x.plan_date = %s)
      AND snap2.nx_order_numb = s.nx_order_numb
      AND snap2.nx_sku = s.nx_sku
      AND ISNULL(snap2.nx_rel_numb, '') = ''
LEFT JOIN dbo.pc_ship_plan_line pl
       ON pl.plan_date = %s
      AND pl.nx_order_numb = s.nx_order_numb
      AND pl.nx_rel_numb   = s.nx_rel_numb
      AND pl.nx_item_no    = CAST(s.nx_item_no AS VARCHAR(3))
WHERE CAST(s.nx_ddate_invoice AS DATE) = %s
ORDER BY CASE WHEN sh.detected_at IS NULL THEN 1 ELSE 0 END, sh.detected_at,
         s.nx_order_numb, s.nx_rel_numb, s.nx_item_no
"""


def _plan_state(r, was_planned):
  st = r.get('line_status')
  if st in _ACTIVE_PLAN:
    return 'Stretch' if (r.get('plan_tier') == 'Stretch') else 'Planned'
  if st:
    return st
  if was_planned:
    return 'Planned'
  return 'Unplanned'


@anvil.server.callable
def orders_shipped_get_day(day):
  """One round trip: the day's published-plan header, the e21-shipped lines
  in the order they were detected, running total, totals. Same payload
  shape as wo_completed_get_day so the forms share a row template."""
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
    cursor.execute("SELECT MAX(nx_last_updated) AS feed_last_updated FROM dbo.sales_orders_ship")
    feed = (cursor.fetchone() or {}).get('feed_last_updated')
    cursor.execute(_ROWS_SQL, (day, day, day, day, day, day))
    raw = [serialize_row(r) for r in cursor.fetchall()]
  except Exception as e:
    return {'status': 'error', 'message': f'Shipped load error: {e}'}
  finally:
    conn.close()

  rows, running = [], 0.0
  tot = {'n': 0, 'n_floor': 0, 'n_e21': 0, 'n_zero': 0, 'n_no_estimate': 0,
         'n_no_wo': 0, 'n_stock_type': 0,
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
    state = _plan_state(r, was_planned)
    on_plan = state in ('Planned', 'Stretch')
    basis = ('floor' if r.get('floor_complete_at') else
             'e21' if r.get('shipped_detected_at') else 'invoice')
    tot['n'] += 1
    if r.get('work_order_number') is None:
      tot['n_no_wo'] += 1
    if (r.get('part_type') or '') in ('CV', 'BV', 'MS'):
      tot['n_stock_type'] += 1
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
      'done_at': r.get('shipped_detected_at') or r.get('invoice_date'),   # row's Time column
      'line_label': _line_label(r['nx_order_numb'], r.get('nx_rel_numb'), r.get('nx_item_no')),
      'customer': _customer_label(r.get('nx_cust_name'), r.get('nx_cust_city'),
                                  r.get('nx_cust_state'), r.get('shipto_code')),
      'status': (r.get('status') or 'Shipped') + ('' if r.get('work_order_number') else '  · no WO'),
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
          'feed_last_updated': make_serializable(feed),
          'totals': tot,
          'rows': rows}