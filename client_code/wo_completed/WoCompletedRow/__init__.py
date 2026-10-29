from ._anvil_designer import WoCompletedRowTemplate
from anvil import *


class WoCompletedRow(WoCompletedRowTemplate):
  """One completed work order (wo-1.0.0). Designer components:
       lbl_time     'HH:MM' the work counted (floor Complete or e21 shipped)
       lbl_line     order-release-item          (bold)
       lbl_what     SKU
       lbl_sub      description · qty · WO number
       lbl_status   e21/WO status ('Shipped - Verify', 'Complete', ...)
       lbl_plan     Planned / Stretch / Unplanned / Removed / Unplaced  (+ Z)
       lbl_load     work-inches for this line ('—' when no estimate, 'Z' zero-touch)
       lbl_running  running total after this line
  Colors: DAYLIGHT text steps (#006300 green, #a16207 amber, #C62828 red)."""

  def __init__(self, **properties):
    self.init_components(**properties)
    it = self.item
    self.lbl_time.text = (it.get('done_at') or '')[11:16]
    self.lbl_time.tooltip = {'tablet': 'the Assembler tablet: Complete tapped on the sales order',
                             'step': 'the Assemble routing step completed from the floor',
                             'floor': 'closed on the floor (Complete event)',
                             'e21': 'e21 shows the line shipped (import detection)',
                             'completed_at': 'work order completed_at',
                             'ship': 'shipping clerk pressed Ship in e21',
                             'invoice': 'e21 invoice date (no time)'}.get(it.get('basis'), '')
    self.lbl_line.text = it.get('line_label') or it.get('nx_order_numb') or ''
    self.lbl_line.bold = True
    self.lbl_what.text = it.get('nx_sku') or ''
    sub = it.get('sku_desc') or ''
    q = it.get('qty')
    if q is not None:
      sub += f"  · qty {float(q):g}"
      qo = it.get('qty_ordered')                 # ship-1.1.2: shipped rows carry ordered too
      if qo is not None and float(qo) != float(q):
        sub += f" of {float(qo):g}"
    if it.get('work_order_number'):
      sub += f"  · {it['work_order_number']}"
    who = it.get('done_by') or it.get('assigned_assembler') or ''   # wo-1.2.0: who tapped Complete
    if who:
      sub += f"  · {who}"
    if it.get('customer'):                       # ship-1.0.0: shipped rows carry the customer
      sub += f"  · {it['customer']}"
    self.lbl_sub.text = sub
    self.lbl_status.text = it.get('status') or ''
    self.lbl_status.foreground = '#006300' if it.get('status') == 'Complete' else '#595959'

    state = it.get('plan_state') or 'Unplanned'
    ptxt = state + ('  Z' if it.get('zero_touch') else '')
    self.lbl_plan.text = ptxt
    self.lbl_plan.foreground = ('#006300' if state in ('Planned', 'Stretch')
                                else '#a16207' if state == 'Unplanned' else '#898781')
    self.lbl_plan.tooltip = ('on the published plan' if state in ('Planned', 'Stretch')
                             else 'shipped without being on the plan' if state == 'Unplanned'
                             else f'plan line was {state}')

    load = it.get('load_inches')
    if it.get('zero_touch'):
      self.lbl_load.text = 'Z'
      self.lbl_load.foreground = '#898781'
      self.lbl_load.tooltip = 'zero-touch: plans free against the cap; not in the running total'
    elif load is None:
      self.lbl_load.text = '—'
      self.lbl_load.foreground = '#898781'
      self.lbl_load.tooltip = 'no load estimate: the line was not open at publish time'
    else:
      self.lbl_load.text = f"{float(load):,.1f} in"
      self.lbl_load.foreground = None
    self.lbl_running.text = f"{float(it.get('running_inches') or 0):,.0f}"
    self.lbl_running.bold = True
