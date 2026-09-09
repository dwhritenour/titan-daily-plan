from ._anvil_designer import ShipPlanLineRowTemplate
from anvil import *
import anvil.server


# Status chips: (background, foreground) — DAYLIGHT steps (light Board Mode
# palette, green-lit 2026-08-24). The status WORD is always shown — color is
# reinforcement only, never the sole signal (house pattern, RoutingStatusRow).
STATUS_COLORS = {
  'Planned':   ('#E8F5E9', '#1B5E20'),   # pale green / dark green
  'Unplaced':  ('#D03B3B', '#FFFFFF'),   # critical red / white
  'Snoozed':   ('#F0EFEA', '#52514E'),   # warm light gray / secondary ink
  'Removed':   ('#F7F6F2', '#898781'),   # near-page / muted
  'Proposed':  ('#E3F2FD', '#1565C0'),   # Wk-3: pale blue / medium blue
  'Confirmed': ('#E8F5E9', '#1B5E20'),   # Wk-3
}

# A PLANNED line's chip shows its TIER instead — the word 'Planned' says
# less than Must / Next / Stretch does. Word + color, never color alone.
TIER_COLORS = {
  'Must':    ('#1565C0', '#FFFFFF'),   # strong blue / white — commands the eye
  'Next':    ('#E3F2FD', '#1565C0'),   # pale blue / medium blue
  'Stretch': ('#F0EFEA', '#52514E'),   # warm gray — "if capacity permits"
}


class ShipPlanLineRow(ShipPlanLineRowTemplate):
  """One plan line. Designer components: lbl_order, lbl_what, lbl_sub,
  lbl_qty, lbl_shipby, lbl_load, lbl_status, lk_drift,
  btn_refresh, btn_snooze, btn_remove
  (+ btn_priority '★' — hand-added in the designer, hasattr-guarded).

  DRIFT RULE: lk_drift only DISPLAYS the planned-vs-live gap. Clicking it
  runs the same explicit Refresh as btn_refresh — nothing re-totals
  without a click, and the server logs Recalc with old/new."""

  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item
    status = item.get('line_status', 'Planned')

    self.lbl_order.text = item['nx_order_numb']
    self.lbl_what.text = item['nx_sku']
    sub = item.get('shipto_code') or ''
    if float(item.get('surcharge_minutes') or 0) > 0.5:
      sub += f"  · +{float(item['surcharge_minutes']):,.0f} min surcharge"
    reason_txt = item.get('reason_desc')
    note = item.get('priority_note')
    if note:
      # For OTHER the note IS the reason; otherwise it colors it.
      reason_txt = (note if item.get('priority_reason') == 'OTHER'
                    else f"{reason_txt} — {note}" if reason_txt else note)
    if reason_txt:
      sub += f"  · {reason_txt}"
    if status == 'Proposed' and item.get('proposed_by') == 'agent':
      sub = 'AGENT PROPOSED  · ' + sub      # word, not just the blue chip
    self.lbl_sub.text = sub
    self.lbl_qty.text = f"{float(item['qty_planned']):g}"
    self.lbl_shipby.text = (item.get('ship_by_date') or '')[:10]
    self.lbl_load.text = f"{float(item['load_inches']):,.1f} in"
    self.lbl_load.tooltip = (f"{float(item['load_minutes']):,.0f} min "
                             f"({float(item['surcharge_minutes']):,.0f} surcharge)")

    chip_text = status.upper()
    if status == 'Snoozed' and item.get('snooze_to_date'):
      chip_text = f"→ {item['snooze_to_date'][:10]}"
    bg, fg = STATUS_COLORS.get(status, STATUS_COLORS['Planned'])
    if status == 'Planned':
      tier = item.get('tier') or 'Next'
      bg, fg = TIER_COLORS.get(tier, TIER_COLORS['Next'])
      chip_text = {'Must': 'PRIORITY', 'Next': 'NEXT',
                   'Stretch': 'IF TIME'}.get(tier, tier.upper())
    self.lbl_status.text = chip_text
    self.lbl_status.background = bg
    self.lbl_status.foreground = fg

    # ---- drift badge -------------------------------------------------
    drift = item.get('drift')
    self.lk_drift.visible = drift is not None
    if drift == 'gone':
      self.lk_drift.text = '⚑ no open balance'
      self.lk_drift.tooltip = ('Nothing left open in the feed — likely '
                               'shipped. Consider Remove.')
    elif drift:
      self.lk_drift.text = f'⚑ {drift}'
      self.lk_drift.tooltip = ('Live feed disagrees with the planned '
                               'quantity. Click to re-total (logged as '
                               'Recalc). Plans never re-total on their own.')
    self.lk_drift.foreground = '#a16207'

    # ---- actions (item-callback pattern) -----------------------------
    on_recalc = item.get('on_recalc')
    on_snooze = item.get('on_snooze')
    on_remove = item.get('on_remove')
    active = status in ('Planned', 'Unplaced', 'Proposed', 'Confirmed')

    self.btn_refresh.visible = bool(on_recalc) and active
    self.btn_snooze.visible = bool(on_snooze) and active
    self.btn_remove.visible = bool(on_remove) and status != 'Removed'

    if on_recalc:
      self.btn_refresh.set_event_handler('click',
                                         lambda **e: on_recalc(self.item))
      self.lk_drift.set_event_handler('click',
                                      lambda **e: on_recalc(self.item))
    if on_snooze:
      self.btn_snooze.set_event_handler('click',
                                        lambda **e: on_snooze(self.item))
    if on_remove:
      self.btn_remove.set_event_handler('click',
                                        lambda **e: on_remove(self.item))

    # btn_priority ('★') is hand-added in the designer — guarded so this
    # code runs clean before Dave adds it.
    on_priority = item.get('on_priority')
    if hasattr(self, 'btn_priority'):
      self.btn_priority.visible = bool(on_priority) and active
      if on_priority:
        self.btn_priority.set_event_handler(
          'click', lambda **e: on_priority(self.item))

    # btn_confirm ('✓') is hand-added in the designer — guarded. Visible
    # only on Proposed lines for PC: one tap = Proposed -> Planned.
    on_confirm = item.get('on_confirm')
    if hasattr(self, 'btn_confirm'):
      self.btn_confirm.visible = bool(on_confirm) and status == 'Proposed'
      if on_confirm:
        self.btn_confirm.set_event_handler(
          'click', lambda **e: on_confirm(self.item))