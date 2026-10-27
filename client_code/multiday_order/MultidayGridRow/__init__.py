from ._anvil_designer import MultidayGridRowTemplate
from anvil import *


# =============================================================================
# MultidayGridRow - one row of the Multi-day Work Order page grid (v1.9).
#
# Columns: 'line' (the row label) and the five weekdays 'd1'..'d5' (Mon-Fri
# of the week the page shows). Each weekday column holds a Label (lbl_dN)
# AND a TextBox (tb_dN): the 'assemblers' row shows the TextBox - PC types
# the number of assemblers on this work order for that day straight in the
# grid - every other row shows the Label. Whichever is not used is removed
# from the row (a hidden component still renders a strip in a DataRowPanel).
#
# item = {'kind': 'asm' | 'line' | 'load' | 'tablet', 'label': 'row label',
#         'd1'..'d5': cell text (non-asm rows),
#         'asm': {'d1': {'value': n or None, 'explicit': bool,
#                        'carried_from': 'YYYY-MM-DD' or None, 'editable': bool}, ...},
#         'today_slot': 'd2' or None,
#         'on_asm': callback(slot, text)   # the page saves it}
# =============================================================================

MUTED = '#595959'
SLOTS = ('d1', 'd2', 'd3', 'd4', 'd5')


class MultidayGridRow(MultidayGridRowTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item or {}
    kind = item.get('kind') or 'line'
    today_slot = item.get('today_slot')
    is_asm = kind == 'asm'
    is_line = kind == 'line'
    on_asm = item.get('on_asm')

    self.lbl_line.text = item.get('label') or ''
    self.lbl_line.bold = is_line or is_asm
    self.lbl_line.foreground = '' if (is_line or is_asm) else MUTED
    self.lbl_line.italic = not (is_line or is_asm)

    for slot in SLOTS:
      lbl = getattr(self, f'lbl_{slot}', None)
      tb = getattr(self, f'tb_{slot}', None)
      if is_asm and tb is not None:
        if lbl is not None:
          lbl.remove_from_parent()
        a = (item.get('asm') or {}).get(slot) or {}
        v = a.get('value')
        tb.visible = True
        tb.enabled = bool(a.get('editable')) and bool(on_asm)
        if a.get('explicit') and v is not None:
          tb.text = f"{v:g}"
          tb.placeholder = ''
        else:
          tb.text = ''
          tb.placeholder = (f"{v:g} (carried)" if v is not None else 'pool')
        tb.tooltip = ('assemblers on this work order that day - blank carries the previous day, 0 = nobody'
                      if a.get('editable') else 'past day - what was planned')
        tb.tag = slot
        if on_asm and a.get('editable'):
          tb.set_event_handler('change', self._changed)
          tb.set_event_handler('pressed_enter', self._changed)
        continue
      if tb is not None:
        tb.remove_from_parent()
      if lbl is None:
        continue
      txt = item.get(slot)
      lbl.text = txt if txt not in (None, '') else '-'
      lbl.bold = (slot == today_slot) and is_line
      lbl.foreground = '' if is_line else MUTED
      lbl.italic = (not is_line) or (isinstance(txt, str) and ('est.' in txt))

    if is_line:
      self.lbl_line.tooltip = 'planned = on a plan that day; counted = assembly tablet Count; ~ est. = what the 06:30 run would propose with the assemblers above'
    elif is_asm:
      self.lbl_line.tooltip = "PC's number per day. Blank = the previous day's number carries; 0 = nobody on it that day; no number anywhere = the pool rule."
    elif kind == 'load':
      self.lbl_line.tooltip = 'work inches on the plan that day; est. = projected'
    elif kind == 'tablet':
      self.lbl_line.tooltip = 'assembly-operator taps that day (Start / Count / Stop / Restart / Complete)'

  def _changed(self, sender, **event_args):
    on_asm = (self.item or {}).get('on_asm')
    if on_asm:
      on_asm(sender.tag, sender.text)