from ._anvil_designer import ship_planTemplate
from anvil import *
import anvil.server
from datetime import date, datetime, timedelta

from .ShipPlanParamsPanel import ShipPlanParamsPanel


class ship_plan(ship_planTemplate):
  """
    Daily Ship Plan — Week-2 release (PC builds/publishes; Sales read-only).
    Spec: documents/Ship_Plan_Form_Specification.docx v1.0.0.

    Designer components this code binds by name (§2 of the spec):
      oc_header:    dp_plan_date, lbl_plan_status, btn_publish,
                    lk_history, lk_params
      oc_capacity:  lbl_capacity_line, cv_capacity (Canvas, ~28px tall,
                    full width — the bar is DRAWN, so it needs no CSS),
                    lbl_crew, lbl_snapshot_warn, lk_overrides
      flow_filters: tb_search, dd_bucket, cb_due_window, cb_special_only,
                    cb_defaulted_only, lbl_feed_note
      grids:        rp_candidates (ShipPlanCandidateRow),
                    rp_plan_lines (ShipPlanLineRow), cb_show_removed
      timer:        timer_drift (interval 300)

    The client computes NOTHING — capacity, loads, classification and the
    already-planned guard are all server/view-side. Drift is only ever
    DISPLAYED here; the Refresh action is the one re-total path.
  """

  def __init__(self, **properties):
    self.init_components(**properties)

    # --- role gate (Wk-2: Sales = read-only) -------------------------
    # TODO(Dave/Chuck): wire to the app's real role source, same pattern
    # as the other PC forms. is_pc=False hides every mutating control.
    self.is_pc = True

    for c in (self.btn_publish, self.lk_params, self.lk_overrides):
      c.visible = self.is_pc

    # --- event wiring -------------------------------------------------
    self.dp_plan_date.set_event_handler('change', self._date_changed)
    self.btn_publish.set_event_handler('click', self._publish_click)
    self.lk_history.set_event_handler('click', self._history_click)
    self.lk_params.set_event_handler('click', self._params_click)
    self.lk_overrides.set_event_handler('click', self._overrides_click)
    self.tb_search.set_event_handler('pressed_enter', self._filters_changed)
    self.dd_bucket.set_event_handler('change', self._filters_changed)
    for cb in (self.cb_due_window, self.cb_special_only,
               self.cb_defaulted_only, self.cb_show_removed):
      cb.set_event_handler('change', self._filters_changed)
    self.timer_drift.set_event_handler('tick', self._timer_tick)
    # Canvas fires 'reset' when it (re)sizes — redraw the bar then.
    self.cv_capacity.set_event_handler('reset', self._draw_capacity_bar)

    self.dd_bucket.items = [('All product', None), ('Strainers', 'ST'),
                            ('Zero-touch', 'ZERO'), ('Other', 'OTHER')]
    self.cb_due_window.checked = True

    self._horizon_days = 10          # refreshed from parameters on load
    self._capacity = None
    self._lines = []

    self.dp_plan_date.date = self._next_business_day()
    self.load_day()

  # =================================================================
  # Loading
  # =================================================================
  @staticmethod
  def _next_business_day():
    d = date.today()
    while d.weekday() >= 5:
      d += timedelta(days=1)
    return d

  @property
  def plan_date(self):
    return self.dp_plan_date.date or self._next_business_day()

  def load_day(self):
    result = anvil.server.call('ship_plan_get_plan', self.plan_date)
    if result.get('status') != 'success':
      alert(f"Error loading plan: {result.get('message', 'Unknown error')}")
      return
    self._capacity = result['capacity']
    self._render_header(result.get('header'))
    self._render_capacity(result['planned_inches'])
    self._render_lines(result['lines'])
    self.refresh_candidates()

  def refresh_candidates(self, **event_args):
    result = anvil.server.call(
      'ship_plan_get_candidates',
      search=self.tb_search.text or None,
      bucket=self.dd_bucket.selected_value,
      due_within_days=self._horizon_days if self.cb_due_window.checked else None,
      special_only=self.cb_special_only.checked,
      defaulted_only=self.cb_defaulted_only.checked)
    if result.get('status') != 'success':
      alert(f"Error loading candidates: {result.get('message')}")
      return
    items = result['candidates']
    for it in items:
      it['on_add'] = self._add_line if self.is_pc else None
    self.rp_candidates.items = items

  _filters_changed = refresh_candidates

  def _date_changed(self, **event_args):
    self.load_day()

  # =================================================================
  # Rendering
  # =================================================================
  def _render_header(self, header):
    # Colors are the DAYLIGHT status steps (light Board Mode palette,
    # green-lit 2026-08-24): green/amber as TEXT use the dark steps
    # (#006300 / #a16207) so they hold contrast on the light surface;
    # the bright fills (#0ca30c / #fab219) are reserved for the meter.
    if header is None:
      self.lbl_plan_status.text = 'NO PLAN YET'
      self.lbl_plan_status.foreground = '#898781'
      self.btn_publish.enabled = False
      return
    status = header['plan_status']
    self.lbl_plan_status.text = status.upper()
    if status == 'Published':
      self.lbl_plan_status.foreground = '#006300'
      self.lbl_plan_status.tooltip = (
        f"published by {header.get('published_by')} "
        f"at {header.get('published_at')}")
    else:
      self.lbl_plan_status.foreground = '#a16207'
    self.btn_publish.enabled = self.is_pc and status == 'Draft'

  def _render_capacity(self, planned):
    cap = self._capacity or {}
    eff_cap = cap.get('capacity_inches_effective') or 0
    self._planned = planned
    self.lbl_capacity_line.text = (
      f"Planned {planned:,.0f} of {eff_cap:,.0f} work-inches")
    self._draw_capacity_bar()
    self.lbl_crew.text = cap.get('capacity_detail', '')
    stale = cap.get('snapshot_stale') and not cap.get('is_estimate')
    self.lbl_snapshot_warn.visible = bool(stale)
    if stale:
      self.lbl_snapshot_warn.text = (
        f"⚠ Attendance snapshot is from {cap.get('snapshot_date')} — "
        "morning file not loaded.")

  def _draw_capacity_bar(self, **event_args):
    """Draw the capacity meter on cv_capacity. Green < 85%, amber 85-100%,
    red over. Dashed outline = future-date ESTIMATE (no snapshot yet)."""
    cap = self._capacity or {}
    eff_cap = cap.get('capacity_inches_effective') or 0
    planned = getattr(self, '_planned', 0)
    ratio = (planned / eff_cap) if eff_cap else 0

    c = self.cv_capacity
    w, h = c.get_width(), c.get_height()
    if not w or not h:
      return
    c.clear_rect(0, 0, w, h)
    c.fill_style = '#e7e5df'                       # track (light surface-2)
    c.fill_rect(0, 0, w, h)
    fill_w = min(ratio, 1.0) * w
    c.fill_style = ('#d03b3b' if ratio > 1.0
                    else '#fab219' if ratio >= 0.85
                    else '#2a78d6')                # series blue, light step
    c.fill_rect(0, 0, fill_w, h)
    # threshold ticks at 85% and 100%
    c.fill_style = '#fab219'
    c.fill_rect(w * 0.85 - 1, 0, 2, h)
    c.fill_style = '#d03b3b'
    c.fill_rect(w - 2, 0, 2, h)
    if cap.get('is_estimate'):                     # hollow = estimate
      c.stroke_style = '#898781'
      c.line_width = 2
      try:
        c.set_line_dash([6, 4])
      except Exception:
        pass                                        # older runtimes: solid
      c.stroke_rect(1, 1, w - 2, h - 2)

  def _render_lines(self, lines):
    self._lines = lines
    shown = [l for l in lines
             if self.cb_show_removed.checked or l['line_status'] != 'Removed']
    for it in shown:
      if self.is_pc:
        it['on_recalc'] = self._recalc_line
        it['on_snooze'] = self._snooze_line
        it['on_remove'] = self._remove_line
      else:
        it['on_recalc'] = it['on_snooze'] = it['on_remove'] = None
    self.rp_plan_lines.items = shown

  # =================================================================
  # Actions (all PC-only; item-callback pattern like routing_status)
  # =================================================================
  def _add_line(self, item):
    result = anvil.server.call('ship_plan_add_line', self.plan_date,
                               item['nx_order_numb'], item['nx_sku'])
    if result.get('status') != 'success':
      alert(result.get('message', 'Add failed'))
      return
    if result.get('over_capacity'):
      self._cap_hit_dialog(item, result)
    self.load_day()

  def _cap_hit_dialog(self, item, result):
    """DECISION §4: snooze-to-suggested-day, explicit Unplaced, or keep —
    never a silent drop. The line is already added when we get here."""
    sug = result.get('suggested_day')
    msg = (f"This puts {self.plan_date} at "
           f"{result['planned_inches']:,.0f} / "
           f"{result['capacity_inches']:,.0f} work-inches "
           f"(+{result['line_inches']:,.0f} from {item['nx_order_numb']}).")
    buttons = []
    if sug:
      buttons.append((f"Snooze to {sug}", 'snooze'))
    buttons += [("Leave Unplaced", 'unplaced'), ("Keep anyway", 'keep')]
    choice = alert(msg, title="Over capacity", buttons=buttons, large=True)
    order, sku = item['nx_order_numb'], item['nx_sku']
    if choice == 'snooze' and sug:
      r = anvil.server.call('ship_plan_snooze_line', self.plan_date,
                            order, sku, sug)
      if r.get('status') != 'success':
        alert(r.get('message', 'Snooze failed'))
    elif choice == 'unplaced':
      r = anvil.server.call('ship_plan_mark_unplaced', self.plan_date,
                            order, sku, 'over capacity at add')
      if r.get('status') != 'success':
        alert(r.get('message', 'Unplace failed'))
    # 'keep' (or dialog dismissed): stays Planned, bar shows red — PC's call.

  def _recalc_line(self, item):
    r = anvil.server.call('ship_plan_recalc_line', self.plan_date,
                          item['nx_order_numb'], item['nx_sku'])
    if r.get('status') != 'success':
      alert(r.get('message', 'Refresh failed'))
    else:
      Notification(f"Re-totaled: {r['old']} → {r['new']}",
                   timeout=4).show()
    self.load_day()

  def _snooze_line(self, item):
    tb = TextBox(text=item.get('snooze_suggestion') or '',
                 placeholder='YYYY-MM-DD')
    if not confirm(f"Snooze {item['nx_order_numb']} / {item['nx_sku']} "
                   "to which day?", content=tb):
      return
    try:
      to_date = datetime.strptime((tb.text or '').strip(), '%Y-%m-%d').date()
    except ValueError:
      alert('Enter a date as YYYY-MM-DD.')
      return
    r = anvil.server.call('ship_plan_snooze_line', self.plan_date,
                          item['nx_order_numb'], item['nx_sku'], to_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'Snooze failed'))
    self.load_day()

  def _remove_line(self, item):
    tb = TextBox(placeholder='reason (optional)')
    if not confirm(f"Remove {item['nx_order_numb']} / {item['nx_sku']} "
                   "from this plan?", content=tb):
      return
    r = anvil.server.call('ship_plan_remove_line', self.plan_date,
                          item['nx_order_numb'], item['nx_sku'],
                          tb.text or None)
    if r.get('status') != 'success':
      alert(r.get('message', 'Remove failed'))
    self.load_day()

  def _publish_click(self, **event_args):
    cap = self._capacity or {}
    planned = sum(float(l['load_inches']) for l in self._lines
                  if l['line_status'] in ('Planned', 'Proposed', 'Confirmed'))
    if not confirm(
      f"Publish {self.plan_date}?\n\n"
      f"Planned {planned:,.0f} of "
      f"{cap.get('capacity_inches_effective', 0):,.0f} work-inches.\n"
      f"{cap.get('capacity_detail', '')}\n\n"
      "The capacity number is stamped on the plan — it's the number "
      "sales sees."):
      return
    r = anvil.server.call('ship_plan_publish', self.plan_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'Publish failed'))
    self.load_day()

  # =================================================================
  # Panels / history / timer
  # =================================================================
  def _params_click(self, **event_args):
    alert(ShipPlanParamsPanel(override_date=self.plan_date),
          title='Ship Plan parameters', large=True, buttons=[('Close', None)])
    self.load_day()

  def _overrides_click(self, **event_args):
    alert(ShipPlanParamsPanel(override_date=self.plan_date,
                              section='overrides'),
          title=f'Assembler overrides — {self.plan_date}',
          large=True, buttons=[('Close', None)])
    self.load_day()

  def _history_click(self, **event_args):
    r = anvil.server.call('ship_plan_get_events', self.plan_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'History load failed'))
      return
    lines = []
    for e in r['events']:
      what = f"{e['nx_order_numb']}/{e['nx_sku']}" if e['nx_order_numb'] else ''
      change = ''
      if e['old_value'] and e['new_value']:
        change = f": {e['old_value']} → {e['new_value']}"
      elif e['new_value']:
        change = f": {e['new_value']}"
      lines.append(f"{str(e['event_time'])[11:16]}  {e['event_type']:<11} "
                   f"{what}{change}  ({e['actor']})")
    ta = TextArea(text='\n'.join(lines) or 'No events yet.', height=400)
    ta.enabled = False
    alert(ta, title=f'Plan history — {self.plan_date}', large=True,
          buttons=[('Close', None)])

  def _timer_tick(self, **event_args):
    """READ-ONLY refresh: drift badges + the bar's planned total only.
    Never calls recalc (drift rule)."""
    result = anvil.server.call('ship_plan_get_plan', self.plan_date)
    if result.get('status') != 'success':
      return
    self._capacity = result['capacity']
    self._render_capacity(result['planned_inches'])
    self._render_lines(result['lines'])