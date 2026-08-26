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
    ⚠ DEF  = classification defaulted conservative (PC review — 002_pratik)
    S      = special elements present
    Z      = zero-touch (plans free against the cap)
    $      = surcharge applies (SpaceX / Termotran / duplex / mount)
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

    flags = []
    if item.get('n_class_defaulted'):
      flags.append('⚠ DEF')
    if item.get('n_special'):
      flags.append('S')
    if item.get('bucket') == 'ZERO':
      flags.append('Z')
    if float(item.get('surcharge_minutes') or 0) > 0.5:
      flags.append('$')
    self.lbl_flags.text = '  '.join(flags)
    if item.get('n_class_defaulted'):
      self.lbl_flags.foreground = '#a16207'
      self.lbl_flags.tooltip = ('Classification missing at plan time — '
                                'charged conservative (Special)')

    on_add = item.get('on_add')
    self.btn_add.visible = on_add is not None
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