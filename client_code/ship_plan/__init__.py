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
      row buttons (hand-added in the designer, hasattr-guarded until
                    then): candidate row btn_add_order ('+SO'),
                    line row btn_priority ('★')
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
    self._render_capacity(result['planned_inches'],
                          result.get('stretch_inches', 0))
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
      it['on_add_order'] = self._add_order if self.is_pc else None
    self.rp_candidates.items = items
    self._render_feed_chip(result)

  def _render_feed_chip(self, result):
    """ALWAYS-ON orders-feed freshness chip (server-computed age, so no
    browser-clock skew). Fresh <= 120 min; stale beyond that."""
    age = result.get('feed_age_minutes')
    ts = (result.get('feed_last_updated') or '')[11:16]
    self.lbl_feed_note.visible = True
    if age is None:
      self.lbl_feed_note.text = 'Orders ⚠ feed empty'
      self.lbl_feed_note.foreground = '#a16207'
    elif age > 120:
      hrs = age / 60.0
      self.lbl_feed_note.text = f'Orders ⚠ {hrs:.1f} h old ({ts})'
      self.lbl_feed_note.foreground = '#a16207'
    else:
      self.lbl_feed_note.text = f'Orders ✓ {ts}'
      self.lbl_feed_note.foreground = '#006300'

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
    txt = status.upper()
    if status == 'Published':
      # "PUBLISHED 8:52 · rev 3 (10:14)" — post-publish edits are fine
      # (real life happens) but they are VISIBLE, never silent. The
      # counter lives server-side (_bump_revision); Draft edits are free.
      pub_t = str(header.get('published_at') or '')[11:16]
      if pub_t:
        txt = f'PUBLISHED {pub_t}'
      rev = header.get('revision_no') or 0
      if rev:
        rev_t = str(header.get('last_revised_at') or '')[11:16]
        txt += f' · rev {rev}' + (f' ({rev_t})' if rev_t else '')
      self.lbl_plan_status.foreground = '#006300'
      self.lbl_plan_status.tooltip = (
        f"published by {header.get('published_by')} "
        f"at {header.get('published_at')}")
    else:
      self.lbl_plan_status.foreground = '#a16207'
    self.lbl_plan_status.text = txt
    self.btn_publish.enabled = self.is_pc and status == 'Draft'

  def _render_capacity(self, planned, stretch=0):
    cap = self._capacity or {}
    eff_cap = cap.get('capacity_inches_effective') or 0
    self._planned = planned
    self._stretch = stretch or 0
    txt = f"Planned {planned:,.0f} of {eff_cap:,.0f} work-inches"
    if self._stretch:
      # Stretch is "if capacity permits" — it never counts against the
      # cap, so it rides OUTSIDE the Planned number and the meter fill.
      txt += f"  · +{self._stretch:,.0f} stretch"
    self.lbl_capacity_line.text = txt
    self._draw_capacity_bar()
    self.lbl_crew.text = cap.get('capacity_detail', '')
    # ALWAYS-ON attendance freshness chip (Dave 2026-08-25): positive
    # confirmation beats an appearing warning — a missing chip would be
    # indistinguishable from "all good". Word + color, never color alone.
    self.lbl_snapshot_warn.visible = True
    if cap.get('is_estimate'):
      self.lbl_snapshot_warn.text = 'Attendance — estimate (no snapshot yet)'
      self.lbl_snapshot_warn.foreground = '#898781'   # muted
    elif cap.get('snapshot_stale'):
      self.lbl_snapshot_warn.text = (
        f"Attendance ⚠ {cap.get('snapshot_date')} — morning file not loaded")
      self.lbl_snapshot_warn.foreground = '#a16207'   # amber text step
    else:
      self.lbl_snapshot_warn.text = (
        f"Attendance ✓ {cap.get('snapshot_date') or 'today'}")
      self.lbl_snapshot_warn.foreground = '#006300'   # green text step

  def _draw_capacity_bar(self, **event_args):
    """Capacity meter, fully DRAWN (no CSS — standard-compliant).
    Normal day: blue fill, faint amber watch zone from 85% to the cap,
    planned value printed at the data end, headroom above, named ticks
    below. Over capacity: amber to the cap, then a red overflow segment
    with '+N over'. Future-day estimate: light-blue 45-degree hatched
    fill labeled 'estimate'. Set cv_capacity HEIGHT to 64 in the
    designer for the full layout — below 60px it draws the bar only."""
    import math
    cap = self._capacity or {}
    eff_cap = cap.get('capacity_inches_effective') or 0
    planned = getattr(self, '_planned', 0)
    ratio = (planned / eff_cap) if eff_cap else 0
    over = ratio > 1.0 and eff_cap > 0
    est = bool(cap.get('is_estimate'))

    c = self.cv_capacity
    w, h = c.get_width(), c.get_height()
    if not w or not h:
      return
    c.clear_rect(0, 0, w, h)

    full = h >= 60                       # room for captions + headroom text
    top = 18 if full else 0
    bot = 16 if full else 0
    # The BAR keeps meter proportions no matter the canvas height — extra
    # height becomes breathing room around a ~32px meter, never a thicker
    # slab (2026-08-25: at height 110 the old math drew a 76px brick).
    bh = min(h - top - bot, 32)
    y_off = max(0, (h - (top + bh + bot)) // 2)
    y0 = y_off + top
    y1 = y0 + bh
    rad = max(4, min(10, bh // 3))
    mid = (y0 + y1) / 2

    def rrect(x0, x1, round_right=True, round_left=True):
      """Filled rect from x0..x1 with optionally rounded corners."""
      if x1 - x0 < 1:
        return
      rr = min(rad, (x1 - x0) / 2)
      rl = rr if round_left else 0
      rt = rr if round_right else 0
      c.begin_path()
      c.move_to(x0 + rl, y0)
      c.line_to(x1 - rt, y0)
      if rt:
        c.arc(x1 - rt, y0 + rt, rt, -math.pi / 2, 0)
      c.line_to(x1, y1 - rt)
      if rt:
        c.arc(x1 - rt, y1 - rt, rt, 0, math.pi / 2)
      c.line_to(x0 + rl, y1)
      if rl:
        c.arc(x0 + rl, y1 - rl, rl, math.pi / 2, math.pi)
      c.line_to(x0, y0 + rl)
      if rl:
        c.arc(x0 + rl, y0 + rl, rl, math.pi, 3 * math.pi / 2)
      c.close_path()
      c.fill()

    # Cap position: full width normally; reserve an overflow lane when over.
    cap_x = w * 0.86 if over else w - 2
    zx = cap_x * 0.85                    # 85% watch threshold

    # Track + watch-zone tint (85% -> cap)
    c.fill_style = '#efeeea'
    rrect(0, cap_x)
    c.fill_style = '#f7ecd4'
    c.fill_rect(zx, y0, max(0, cap_x - zx - rad), bh)
    rrect(max(zx, cap_x - 2 * rad), cap_x, round_left=False)

    ov_w = 0
    if est:
      # Estimate: light-blue hatched fill — 'not real yet'
      fx = min(ratio, 1.0) * cap_x
      c.fill_style = '#9ec5f4'
      rrect(0, fx, round_right=False)
      c.stroke_style = '#2a78d6'
      c.line_width = 2
      hx = 4
      while hx + bh < fx:
        c.begin_path()
        c.move_to(hx, y1 - 1)
        c.line_to(hx + bh, y0 + 1)
        c.stroke()
        hx += 12
    elif over:
      # Amber to the cap, 2px gap, red overflow segment sized by overage
      c.fill_style = '#fab219'
      rrect(0, cap_x, round_right=False)
      ov_w = min((ratio - 1.0) * cap_x, w - cap_x - 6)
      if ov_w > 3:
        c.fill_style = '#fcfcfb'
        c.fill_rect(cap_x, y0, 2, bh)
        c.fill_style = '#d03b3b'
        rrect(cap_x + 2, cap_x + 2 + ov_w, round_left=False)
    else:
      fx = ratio * cap_x
      c.fill_style = '#fab219' if ratio >= 0.85 else '#2a78d6'
      rrect(0, fx, round_right=False)
      # Stretch: LIGHTER segment appended after the fill — visible work,
      # but visually "not load" (it doesn't count against the cap).
      stretch = getattr(self, '_stretch', 0)
      if stretch and eff_cap:
        sx = min((planned + stretch) / eff_cap, 1.0) * cap_x
        if sx > fx + 4:
          c.fill_style = '#cfe1f8'
          c.fill_rect(fx + 2, y0 + 2, sx - fx - 4, bh - 4)
      c.fill_style = '#fcfcfb'             # 2px surface gap at the data end
      c.fill_rect(fx, y0, 2, bh)

    if not full:
      return

    # ---- text layer (word + position carry meaning, never color alone) ----
    c.text_baseline = 'middle'
    c.font = 'bold 13px sans-serif'
    if over:
      c.fill_style = '#5b4300'             # cap value inside the amber
      c.text_align = 'right'
      c.fill_text(f"{eff_cap:,.0f}", cap_x - 8, mid)
      overage = f"+{planned - eff_cap:,.0f} over"
      if ov_w > 78:                        # room inside the red segment
        c.fill_style = '#ffffff'
        c.text_align = 'right'
        c.fill_text(overage, cap_x + ov_w - 6, mid)
      else:
        c.fill_style = '#d03b3b'
        c.text_align = 'left'
        c.fill_text(overage, cap_x + 2 + ov_w + 6, mid)
    else:
      fx = min(ratio, 1.0) * cap_x
      txt = f"{planned:,.0f}"
      if fx > 70:
        c.fill_style = '#0d366b' if est else '#ffffff'
        c.text_align = 'right'
        c.fill_text(txt, fx - 8, mid)
      else:
        c.fill_style = '#52514e'
        c.text_align = 'left'
        c.fill_text(txt, fx + 8, mid)

    # threshold ticks, extended past the bar, with small captions below
    c.fill_style = '#fab219'
    c.fill_rect(zx - 1, y0 - 5, 2, bh + 10)
    c.fill_style = '#d03b3b'
    c.fill_rect(cap_x - 1, y0 - 5, 2, bh + 10)
    c.font = '10px sans-serif'
    c.fill_style = '#898781'
    c.text_baseline = 'top'
    c.text_align = 'center'
    c.fill_text('85%', zx, y1 + 5)
    if over:
      c.fill_text(f"cap {eff_cap:,.0f}", cap_x, y1 + 5)
    else:
      c.text_align = 'right'
      c.fill_text(f"cap {eff_cap:,.0f}", w - 2, y1 + 5)

    # headroom (or 'estimate' word) above the right/left end of the bar
    c.font = 'bold 12px sans-serif'
    c.text_baseline = 'bottom'
    if est:
      c.text_align = 'left'
      c.fill_style = '#898781'
      c.fill_text('estimate', 0, y0 - 4)
    if not over and eff_cap:
      c.text_align = 'right'
      c.fill_style = '#898781' if est else '#006300'
      c.fill_text(f"{eff_cap - planned:,.0f} in headroom", w - 2, y0 - 4)

  def _render_lines(self, lines):
    self._lines = lines
    shown = [l for l in lines
             if self.cb_show_removed.checked or l['line_status'] != 'Removed']
    for it in shown:
      if self.is_pc:
        it['on_recalc'] = self._recalc_line
        it['on_snooze'] = self._snooze_line
        it['on_remove'] = self._remove_line
        it['on_priority'] = self._priority_dialog
      else:
        it['on_recalc'] = it['on_snooze'] = it['on_remove'] = None
        it['on_priority'] = None
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
      self._cap_hit_dialog(item['nx_order_numb'], [item['nx_sku']], result)
    self.load_day()

  def _add_order(self, item):
    """Whole sales order onto the day — every open SKU, ONE server
    transaction, one cap dialog for the lot (not one per line)."""
    order = item['nx_order_numb']
    result = anvil.server.call('ship_plan_add_order', self.plan_date, order)
    if result.get('status') != 'success':
      alert(result.get('message', 'Add order failed'))
      return
    added = result.get('added') or []
    skipped = result.get('skipped') or []
    note = f"{order}: {len(added)} item(s) added"
    if skipped:
      note += f", {len(skipped)} already on the plan"
    Notification(note, timeout=4).show()
    if result.get('over_capacity'):
      self._cap_hit_dialog(order, added, result)
    self.load_day()

  def _cap_hit_dialog(self, order, skus, result):
    """DECISION §4: snooze / Mark Stretch / explicit Unplaced / keep —
    never a silent drop. The line(s) are already added when we get here.
    Mark Stretch re-tiers them so they stop counting against the cap —
    "if capacity permits" made literal."""
    sug = result.get('suggested_day')
    what = order if len(skus) != 1 else f"{order} / {skus[0]}"
    msg = (f"This puts {self.plan_date} at "
           f"{result['planned_inches']:,.0f} / "
           f"{result['capacity_inches']:,.0f} work-inches "
           f"(+{result['line_inches']:,.0f} from {what}).")
    buttons = []
    if sug:
      buttons.append((f"Snooze to {sug}", 'snooze'))
    buttons += [("Mark Stretch", 'stretch'),
                ("Leave Unplaced", 'unplaced'), ("Keep anyway", 'keep')]
    choice = alert(msg, title="Over capacity", buttons=buttons, large=True)
    for sku in skus:
      if choice == 'snooze' and sug:
        r = anvil.server.call('ship_plan_snooze_line', self.plan_date,
                              order, sku, sug)
      elif choice == 'stretch':
        r = anvil.server.call('ship_plan_set_priority', self.plan_date,
                              order, sku, tier='Stretch')
      elif choice == 'unplaced':
        r = anvil.server.call('ship_plan_mark_unplaced', self.plan_date,
                              order, sku, 'over capacity at add')
      else:
        return  # 'keep' (or dismissed): stays Planned, bar red — PC's call
      if r.get('status') != 'success':
        alert(r.get('message', 'Action failed'))
        return

  def _priority_dialog(self, item):
    """Tier + reason on one line. The REASON list is sales' vocabulary
    (pc_priority_reasons); the TIER (Must / Next / Stretch) is the
    Daily's call — sales proposes why, PC decides when."""
    if not hasattr(self, '_reasons'):
      r = anvil.server.call('ship_plan_get_reasons')
      ok = r.get('status') == 'success'
      self._reasons = (r.get('reasons') or []) if ok else []
    dd_tier = DropDown(items=[('Must today', 'Must'), ('Next', 'Next'),
                              ('Stretch — if capacity permits', 'Stretch')],
                       selected_value=item.get('tier') or 'Next')
    dd_reason = DropDown(
      items=[('— no reason set —', '')] +
      [(x['description'], x['reason_code']) for x in self._reasons],
      selected_value=item.get('priority_reason') or '')
    pnl = ColumnPanel()
    pnl.add_component(Label(text='Tier', bold=True))
    pnl.add_component(dd_tier)
    pnl.add_component(Label(text='Reason (why it matters)', bold=True))
    pnl.add_component(dd_reason)
    if not confirm(f"Priority — {item['nx_order_numb']} / {item['nx_sku']}",
                   content=pnl):
      return
    r = anvil.server.call('ship_plan_set_priority', self.plan_date,
                          item['nx_order_numb'], item['nx_sku'],
                          tier=dd_tier.selected_value,
                          reason=dd_reason.selected_value)
    if r.get('status') != 'success':
      alert(r.get('message', 'Priority change failed'))
    self.load_day()

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
    order = item['nx_order_numb']
    siblings = [l for l in self._lines
                if l['nx_order_numb'] == order
                and l['line_status'] != 'Removed']
    tb = TextBox(placeholder='reason (optional)')
    cb_all = None
    content = tb
    if len(siblings) > 1:
      # Whole-order removal rides the same dialog — no extra button.
      cb_all = CheckBox(text=f'Remove ALL {len(siblings)} lines of {order}')
      content = ColumnPanel()
      content.add_component(cb_all)
      content.add_component(tb)
    if not confirm(f"Remove {order} / {item['nx_sku']} from this plan?",
                   content=content):
      return
    if cb_all and cb_all.checked:
      r = anvil.server.call('ship_plan_remove_order', self.plan_date,
                            order, tb.text or None)
    else:
      r = anvil.server.call('ship_plan_remove_line', self.plan_date,
                            order, item['nx_sku'], tb.text or None)
    if r.get('status') != 'success':
      alert(r.get('message', 'Remove failed'))
    self.load_day()

  def _publish_click(self, **event_args):
    cap = self._capacity or {}
    planned = sum(float(l['load_inches']) for l in self._lines
                  if l['line_status'] in ('Planned', 'Proposed', 'Confirmed'))
    # Publish gate (Dave 2026-08-25): a label can be missed at 8:50 —
    # a stale attendance snapshot gets its own line in the confirm.
    stale_line = ''
    if cap.get('snapshot_stale') and not cap.get('is_estimate'):
      stale_line = (f"⚠ CAPACITY IS BASED ON ATTENDANCE FROM "
                    f"{cap.get('snapshot_date')} — today's file has not "
                    "loaded. Publish anyway?\n\n")
    if not confirm(
      f"Publish {self.plan_date}?\n\n" + stale_line +
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
      what = ''
      if e['nx_order_numb']:
        what = (f"{e['nx_order_numb']}/{e['nx_sku']}" if e['nx_sku']
                else e['nx_order_numb'])   # order-level events have no SKU
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
    self._render_capacity(result['planned_inches'],
                          result.get('stretch_inches', 0))
    self._render_lines(result['lines'])