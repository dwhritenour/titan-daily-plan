from ._anvil_designer import wo_completedTemplate
from anvil import *
import anvil.server
from datetime import date, datetime, timedelta


class wo_completed(wo_completedTemplate):
  """
    Completed work orders by day (wo-1.0.0, 2026-09-16) - actual vs the
    PUBLISHED plan, with a running work-inch total in the order the work
    was done. Read-only; nothing here mutates.

    Designer components this code binds by name:
      dp_day          DatePicker  (the day; defaults to today)
      btn_prev        Button      '<'   (previous business day)      - optional
      btn_next        Button      '>'   (next business day)          - optional
      lbl_summary     Label       bold headline: actual vs plan
      lbl_plan        Label       plan status / capacity / counts line
      rp_rows         RepeatingPanel, item_template = WoCompletedRow
      lbl_footer      Label       totals line under the grid
      cb_hide_zero    CheckBox    'Hide zero-touch'                  - optional
      lk_back         Link        'Plan board' -> ship_plan           - optional
    Optional ones are hasattr-guarded so the form runs before they exist.

    Day basis = earliest of floor Complete and e21 shipped detection
    (Dave, 2026-09-16). Work-inches = publish-time snapshot, else the day's
    plan line, else '-' (never invented). Zero-touch shown as Z, not totalled.
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
    self.dp_day.date = date.today()
    self.load_day()

  # -----------------------------------------------------------------
  @property
  def day(self):
    return self.dp_day.date or date.today()

  def _step(self, n):
    d = self.day
    while True:
      d += timedelta(days=n)
      if d.weekday() < 5:
        break
    self.dp_day.date = d
    self.load_day()

  def load_day(self):
    r = anvil.server.call('wo_completed_get_day', self.day)
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
    if hdr is None:
      self.lbl_summary.text = (f"{dtxt} — {actual:,.0f} in completed · no plan was built for this day")
      self.lbl_summary.foreground = '#595959'
    else:
      self.lbl_summary.text = (f"{dtxt} — actual {actual:,.0f} in vs plan {planned:,.0f} in"
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
    line.append(f"{t['n']} work orders done: {t['n_floor']} closed on the floor, "
                f"{t['n_e21']} by e21 shipment")
    self.lbl_plan.text = '  ·  '.join(line)
    foot = [f"On plan {float(t['actual_planned_inches']):,.0f} in",
            f"unplanned {float(t['actual_unplanned_inches']):,.0f} in"]
    if t.get('n_zero'):
      foot.append(f"{t['n_zero']} zero-touch (Z, not totalled)")
    if t.get('n_no_estimate'):
      foot.append(f"{t['n_no_estimate']} with no load estimate (born after publish; shown as —)")
    self.lbl_footer.text = '  ·  '.join(foot)

  def _render_rows(self):
    rows = list((self._data or {}).get('rows') or [])
    if hasattr(self, 'cb_hide_zero') and self.cb_hide_zero.checked:
      rows = [x for x in rows if not x.get('zero_touch')]
    self.rp_rows.items = rows