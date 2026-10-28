from ._anvil_designer import MultidayItemRowTemplate
from anvil import *


# =============================================================================
# MultidayItemRow - one open line of the sales order, in the grid under the
# Multi-day Work Order page's controls (v1.9.1, Dave 10-27: "they should be
# in a datagrid below flow_done, with a Load option").
#
# item = {'item', 'sku', 'desc', 'qty', 'remaining', 'done', 'status',
#         'multiday': bool (over the day line or started), 'asm': n or None,
#         'current': bool (the work order the page shows),
#         'on_load': callback(item)}
# =============================================================================

MUTED = '#595959'


class MultidayItemRow(MultidayItemRowTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    it = self.item or {}
    cur = bool(it.get('current'))
    md = bool(it.get('multiday'))
    self.lbl_item.text = ('⧗ ' if md else '') + f"-{it.get('item')}"
    self.lbl_item.bold = cur
    self.lbl_item.tooltip = ('multi-day: over the day line or started' if md else 'fits in a day - planned as a whole line')
    self.lbl_sku.text = f"{it.get('sku') or ''}" + (f"  {it.get('desc')}" if it.get('desc') else '')
    self.lbl_sku.bold = cur
    self.lbl_open.text = f"{float(it.get('qty') or 0):g}"
    self.lbl_left.text = 'built' if it.get('done') else f"{float(it.get('remaining') or 0):g}"
    self.lbl_status.text = it.get('status') or ''
    self.lbl_status.foreground = '' if md else MUTED
    # planned / assigned (124): PC's number for today, then the crew the
    # Supervisor board put on it (* = lead)
    a = it.get('asm')
    n = int(it.get('crew_n') or 0)
    planned_txt = (f"{float(a):g}" if a is not None else ('pool' if md else '-'))
    self.lbl_asm.text = f"{planned_txt} / {n}"
    self.lbl_asm.foreground = ('#C62828' if (a is not None and n < float(a)) else ('' if (a is not None or n) else MUTED))
    self.lbl_asm.tooltip = ('planned / assigned' + (f" · assigned: {it.get('crew_who')}" if it.get('crew_who') else ' · nobody assigned yet')
                            + (' · short of the instruction' if (a is not None and n < float(a)) else ''))
    on_load = it.get('on_load')
    if cur:
      # a Link has no 'enabled' (Anvil): leave it with no click handler
      self.lk_load.text = 'shown'
      self.lk_load.bold = False
      self.lk_load.foreground = MUTED
      self.lk_load.tooltip = 'this is the work order on the page'
    elif on_load:
      self.lk_load.text = 'Load'
      self.lk_load.tooltip = 'show this work order on the page'
      self.lk_load.set_event_handler('click', lambda **e: on_load(it.get('item')))
    else:
      self.lk_load.visible = False