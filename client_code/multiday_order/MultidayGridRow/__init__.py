from ._anvil_designer import MultidayGridRowTemplate
from anvil import *


# =============================================================================
# MultidayGridRow - one row of the Multi-day Order page grid (v1.7.2).
#
# The page's DataGrid (dg_grid) sets its columns at runtime: 'line', up to
# eight day slots 'd1'..'d8' (oldest left; the page picks which days fill
# them), and 'left'. This row carries one Label per slot so the look is
# edited in the designer, not in code (Dave, 10-27). Slots the page did not
# use are removed from the row (not hidden - see the note in the loop).
#
# item = {'kind': 'asm_planned' | 'asm_assigned' | 'line' | 'load' | 'tablet',
#         'label': 'row label', 'd1'..'d8': 'cell text' or None,
#         'left': 'what is left', 'today_slot': 'd2' or None,
#         'used': ['d1', 'd2', ...]}
# =============================================================================

MUTED = '#595959'


class MultidayGridRow(MultidayGridRowTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item or {}
    kind = item.get('kind') or 'line'
    used = item.get('used') or []
    today_slot = item.get('today_slot')
    is_line = kind == 'line'

    self.lbl_line.text = item.get('label') or ''
    self.lbl_line.bold = is_line
    self.lbl_line.foreground = '' if is_line else MUTED
    self.lbl_line.italic = not is_line

    for n in range(1, 9):
      slot = f'd{n}'
      lbl = getattr(self, f'lbl_{slot}', None)
      if lbl is None:
        continue
      if slot not in used:
        # 10-27: the page did not put this slot in dg_grid.columns, and a
        # DataRowPanel shows any component whose column does not exist in a
        # full-width band BELOW the cells - hidden or not, that band is the
        # thick lavender rule Dave saw between rows. Take the Label out.
        lbl.remove_from_parent()
        continue
      txt = item.get(slot)
      lbl.text = txt if txt not in (None, '') else '-'
      lbl.visible = True
      lbl.bold = (slot == today_slot) and is_line
      lbl.foreground = '' if is_line else MUTED
      lbl.italic = (not is_line) or (isinstance(txt, str) and txt.startswith('~'))

    self.lbl_left.text = item.get('left') or ''
    self.lbl_left.foreground = '' if is_line else MUTED
    self.lbl_left.bold = is_line

    if is_line:
      self.lbl_line.tooltip = 'planned = on a plan that day; counted = assembly tablet Count; ~ = what the 06:30 run would propose (est.); (PC) = your override'
    elif kind == 'asm_planned':
      self.lbl_line.tooltip = "PC's headcount for the day (pool = the pool rule, no headcount typed)"
    elif kind == 'asm_assigned':
      self.lbl_line.tooltip = 'from the Supervisor board (work_orders.assigned_assembler) - shown, never changed here'
    elif kind == 'load':
      self.lbl_line.tooltip = 'work inches on the plan that day; est. = projected'
    elif kind == 'tablet':
      self.lbl_line.tooltip = 'assembly-operator taps that day (Start / Count / Stop / Restart / Complete)'