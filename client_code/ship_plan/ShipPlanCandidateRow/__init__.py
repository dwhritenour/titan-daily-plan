from ._anvil_designer import ShipPlanCandidateRowTemplate
from anvil import *
import anvil.server
from datetime import date


class ShipPlanCandidateRow(ShipPlanCandidateRowTemplate):
  """One (order, SKU) group in the candidates pane (releases already
  collapsed by vw_pc_ship_candidates).

  Designer components: lbl_order, lbl_what, lbl_sub, lbl_qty, lbl_shipby,
  lbl_load, lbl_flags, btn_add
  (+ btn_add_order '+SO' — hand-added in the designer, hasattr-guarded).

  Flags column (word + glyph, never color alone):
    ⛔ Xnn  = eligibility gate (hold / stock / no-WO) — row not plannable
    A/B    = urgency / value flag codes (flag system 040/041)
    ⚠ DEF  = classification defaulted conservative (PC review — 002_pratik)
    S      = special elements present
    Z      = zero-touch (plans free against the cap)
    $      = surcharge applies (SpaceX / Termotran / duplex / mount)
  Tooltip lists priority score + every flag as 'code — note' (desktop).
  TAP-TO-SEE: lbl_flags is a LINK in the designer (same name); a tap
  opens a popup with the same breakdown — the Vibe boards have no
  hover. Guarded so the code runs clean while it is still a Label.
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item

    self.lbl_order.text = item['nx_order_numb']
    self.lbl_what.text = f"{item.get('sku_desc') or item['nx_sku']}"
    self.lbl_sub.text = f"{item.get('shipto_code') or ''} · {item.get('part_line') or item.get('bucket', '')}"
    self.lbl_qty.text = f"{item['qty_open']:g}"
    self.lbl_load.text = f"{item['load_inches']:,.1f} in"
    self.lbl_load.tooltip = (f"{item['load_minutes']:,.0f} min total, "
                             f"{item['surcharge_minutes']:,.0f} min surcharge, "
                             f"{item['n_source_rows']} source line(s)")

    ship_by = item.get('ship_by_date')
    self.lbl_shipby.text = (ship_by or '')[:10]
    if ship_by and ship_by[:10] < date.today().isoformat():
      self.lbl_shipby.foreground = '#C62828'   # late — red, light-surface step
      self.lbl_shipby.bold = True

    glyphs = []
    if item.get('n_class_defaulted'):
      glyphs.append('⚠ DEF')
    if item.get('n_special'):
      glyphs.append('S')
    if item.get('bucket') == 'ZERO':
      glyphs.append('Z')
    if float(item.get('surcharge_minutes') or 0) > 0.5:
      glyphs.append('$')
    # Flag system (040/041): gates first, then A/B codes, then glyphs.
    gate_codes = (item.get('gate_codes') or '').replace(',', ' ')
    flag_list = item.get('flags') or []
    ab_codes = ' '.join(f['code'] for f in flag_list
                        if f['code'][:1] in ('A', 'B'))
    parts = []
    if gate_codes:
      parts.append('⛔ ' + gate_codes)
    if ab_codes:
      parts.append(ab_codes)
    if glyphs:
      parts.append('  '.join(glyphs))
    self.lbl_flags.text = ' · '.join(parts)
    score = int(item.get('priority_score') or 0)
    tip_lines = [f'Priority score {score}']
    tip_lines += [f"{f['code']} — {f['note']}" for f in flag_list]
    if item.get('n_class_defaulted'):
      tip_lines.append('DEF — classification missing at plan time; '
                       'charged conservative (Special)')
    self.lbl_flags.tooltip = '\n'.join(tip_lines)
    if item.get('is_gated'):
      # Gated — red (#C62828, light-surface step) + the ⛔ word/glyph,
      # so color is never the only signal.
      self.lbl_flags.foreground = '#C62828'
      self.lbl_flags.bold = True
    elif item.get('n_class_defaulted'):
      self.lbl_flags.foreground = '#a16207'
    self._flag_tip_lines = tip_lines
    # Tap-to-see (touch boards): Link fires 'click'; a Label raises
    # ValueError on this call — swallowed so nothing breaks pre-swap.
    try:
      self.lbl_flags.set_event_handler('click', self._show_flag_details)
    except (ValueError, AttributeError):
      pass

    # Gated rows are not plannable — the line-level add is hidden.
    # (+SO stays visible: adding the ORDER is a PC judgment call on a
    # mixed order — v1 gates are advisory there; a server-side guard
    # in add_order is a candidate once the bake settles behavior.)
    on_add = item.get('on_add')
    self.btn_add.visible = (on_add is not None
                            and not item.get('is_gated'))
    if on_add:
      self.btn_add.set_event_handler('click',
                                     lambda **e: on_add(self.item))

    # btn_add_order ('+SO') is hand-added in the designer — guarded so
    # this code runs clean before Dave adds it.
    on_add_order = item.get('on_add_order')
    if hasattr(self, 'btn_add_order'):
      self.btn_add_order.visible = on_add_order is not None
      if on_add_order:
        self.btn_add_order.set_event_handler(
          'click', lambda **e: on_add_order(self.item))

  def _show_flag_details(self, **event_args):
    """Popup with the flag breakdown for this row — data already in
    hand (no round trip). Fat text for the 75-inch boards."""
    item = self.item
    gated = bool(item.get('is_gated'))
    panel = ColumnPanel()
    panel.add_component(Label(
      text=f"{item['nx_order_numb']} · "
      f"{item.get('sku_desc') or item['nx_sku']}",
      bold=True, font_size=18))
    head = Label(text=self._flag_tip_lines[0], font_size=16)
    if gated:
      head.text += '  —  GATED: not plannable as things stand'
      head.foreground = '#C62828'
      head.bold = True
    panel.add_component(head)
    for f in (item.get('flags') or []):
      lbl = Label(text=f"{f['code']}  {f['name']}", bold=True, font_size=16)
      if f['code'].startswith('X'):
        lbl.foreground = '#C62828'
      panel.add_component(lbl)
      panel.add_component(Label(text=f['note'] or '', font_size=15))
    if item.get('n_class_defaulted'):
      panel.add_component(Label(
        text='DEF  classification missing at plan time — charged '
        'conservative (Special)', foreground='#a16207', font_size=15))
    if not (item.get('flags') or item.get('n_class_defaulted')):
      panel.add_component(Label(text='No flags on this line.', font_size=15))
    alert(content=panel, title='Flags', buttons=[('Close', None)],
          large=False)
