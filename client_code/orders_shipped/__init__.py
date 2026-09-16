from ._anvil_designer import orders_shippedTemplate
from anvil import *
import anvil.server
from datetime import date, datetime, timedelta


class orders_shipped(orders_shippedTemplate):
  """
    Orders shipped by day (ship-1.1.0, 2026-09-16) - what e21 says left the
    building (dbo.sales_orders_ship, invoice date = ship date), against the
    PUBLISHED plan, with a running work-inch total in e21-detection order.
    Twin of wo_completed; that form is the shop's WORK, this one is what
    SHIPPED. Read-only; nothing here mutates.

    Designer components this code binds by name (same set as wo_completed):
      dp_day, btn_prev*, btn_next*, lbl_summary, lbl_plan,
      rp_rows (item_template = wo_completed.WoCompletedRow - shared),
      lbl_footer, cb_hide_zero*, lk_back*        (* optional, hasattr-guarded)

    Work-inches = publish-time snapshot by item (pre-066 days: order + SKU,
    pro-rated by qty), else the day's plan line, else '-'. Zero-touch and
    stock part types (CV/BV/MS) show as Z and are not totalled.
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    self._data = None
    self.dp_day.set_event_handler('change', lambda **e: self.load_day())
    if hasattr(self, 'btn_prev'):
      self.btn_prev.set_event_handler('click', lambda **e: self._step(-1))
    if hasattr(self, 'btn_next'):
      self.btn_next.set_event_handler('click', lambda **e: self._step(1))
    if hasattr(self, 'cb_hide_zero'):
      self.cb_hide_zero.set_event_handler('change', lambda **e: self._render_rows())
    if hasattr(self, 'lk_back'):
      self.lk_back.set_event_handler('click', lambda **e: open_form('ship_plan'))
    # ship-1.1.0: sales_orders_ship is loaded by a NIGHTLY EasyMorph job, so
    # today is always empty until tomorrow - open on the last business day.
    self.dp_day.date = self._prev_business_day(date.today())
    self.load_day()

  # -----------------------------------------------------------------
  @property
  def day(self):
    return self.dp_day.date or date.today()

  @staticmethod
  def _prev_business_day(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
      d -= timedelta(days=1)
    return d

  def _step(self, n):
    d = self.day
    while True:
      d += timedelta(days=n)
      if d.weekday() < 5:
        break
    self.dp_day.date = d
    self.load_day()

  def load_day(self):
    r = anvil.server.call('orders_shipped_get_day', self.day)
    if r.get('status') != 'success':
      alert(r.get('message', 'Load failed'))
      return
    self._data = r
    self._render_header(r)
    self._render_rows()

  # -----------------------------------------------------------------
  def _render_header(self, r):
    t = r['totals']
    hdr = r.get('header')
    planned = float(r.get('planned_inches') or 0)
    actual = float(t.get('actual_inches') or 0)
    pct = r.get('pct_of_plan')
    dtxt = self.day.strftime('%a %m/%d')
    if not r.get('rows') and self.day >= date.today():
      self.lbl_summary.text = (f"{dtxt} — nothing yet: the e21 extract loads overnight; "
                               f"today's shipments appear tomorrow morning")
      self.lbl_summary.foreground = '#595959'
    elif hdr is None:
      self.lbl_summary.text = (f"{dtxt} — {actual:,.0f} in shipped · no plan was built for this day")
      self.lbl_summary.foreground = '#595959'
    else:
      self.lbl_summary.text = (f"{dtxt} — shipped {actual:,.0f} in vs plan {planned:,.0f} in"
                               + (f"  ({pct:,.0f}% of plan)" if pct is not None else ''))
      # DAYLIGHT text steps: green >= 90 %, amber 60-90, red below
      self.lbl_summary.foreground = ('#006300' if (pct or 0) >= 90
                                     else '#a16207' if (pct or 0) >= 60 else '#C62828')
    line = []
    if hdr:
      st = hdr.get('plan_status') or ''
      pub = (hdr.get('published_at') or '')[11:16]
      line.append(f"Plan {st.upper()}" + (f" {pub}" if pub else '')
                  + (f" · rev {hdr.get('revision_no')}" if hdr.get('revision_no') else ''))
      if hdr.get('capacity_inches') is not None:
        line.append(f"capacity {float(hdr['capacity_inches']):,.0f} in")
      line.append(f"{r.get('n_plan_lines', 0)} plan lines, {t.get('n_planned_done', 0)} of them done")
      if float(r.get('stretch_inches') or 0) > 0:
        line.append(f"If-time {float(r['stretch_inches']):,.0f} in not in the plan figure")
    line.append(f"{t['n']} lines shipped per e21"
                + (f", {t['n_no_wo']} with no work order" if t.get('n_no_wo') else '')
                + (f", {t['n_stock_type']} stock part types" if t.get('n_stock_type') else ''))
    if r.get('feed_last_updated'):
      line.append(f"e21 extract {str(r['feed_last_updated'])[:16].replace('T', ' ')}")
    self.lbl_plan.text = '  ·  '.join(line)
    foot = [f"On plan {float(t['actual_planned_inches']):,.0f} in",
            f"unplanned {float(t['actual_unplanned_inches']):,.0f} in"]
    if t.get('n_zero'):
      foot.append(f"{t['n_zero']} zero-touch (Z, not totalled)")
    if t.get('n_no_estimate'):
      foot.append(f"{t['n_no_estimate']} with no load estimate (not open at publish; shown as —)")
    self.lbl_footer.text = '  ·  '.join(foot)

  def _render_rows(self):
    rows = list((self._data or {}).get('rows') or [])
    if hasattr(self, 'cb_hide_zero') and self.cb_hide_zero.checked:
      rows = [x for x in rows if not x.get('zero_touch')]
    self.rp_rows.items = rows