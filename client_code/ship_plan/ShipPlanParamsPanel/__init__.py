from ._anvil_designer import ShipPlanParamsPanelTemplate
from anvil import *
import anvil.server


class ShipPlanParamsPanel(ShipPlanParamsPanelTemplate):
  """
    Parameters panel (alert-form, PC only) — spec §3. Three sections:
      1. Plan parameters   (pc_plan_parameters — value editable)
      2. Special customers (pc_special_customers — add / edit / toggle)
      3. Day overrides     (roster × pc_assembler_day_override for a date)

    Rows are built PROGRAMMATICALLY into three ColumnPanels so this panel
    needs no row templates in the designer — just three empty containers:
      pnl_params, pnl_customers, pnl_overrides
    plus section title labels (lbl_sec_params, lbl_sec_customers,
    lbl_sec_overrides) and lbl_override_date.

    Every save calls its server callable, which writes the ParamChange
    event inside the same transaction.

    section='overrides' opens with only the overrides section visible
    (the capacity bar's "Mark absent / trainee…" link).
  """

  OVERRIDE_TYPES = ['(snapshot)', 'Absent', 'Present', 'Trainee', 'HalfDay']

  def __init__(self, override_date=None, section=None, **properties):
    self.init_components(**properties)
    self.override_date = override_date
    only_overrides = (section == 'overrides')

    for comp in (self.lbl_sec_params, self.pnl_params,
                 self.lbl_sec_customers, self.pnl_customers):
      comp.visible = not only_overrides

    self.lbl_override_date.text = f"Overrides for {override_date}"
    # ONE bundle call (was three sequential calls = three seconds of
    # frozen spinner; every Anvil call pays ~1s transport).
    data = anvil.server.call('ship_plan_get_params_bundle', override_date)
    if data.get('status') != 'success':
      alert(data.get('message', 'Parameter load failed'))
      data = {}
    if not only_overrides:
      self._load_params(data.get('parameters'))
      self._load_customers(data.get('customers'))
    self._load_overrides(data.get('overrides'))

  # =================================================================
  # 1. Plan parameters
  # =================================================================
  def _load_params(self, rows=None):
    self.pnl_params.clear()
    if rows is None:                 # section-only refresh after a save
      r = anvil.server.call('ship_plan_get_parameters')
      if r.get('status') != 'success':
        alert(r.get('message', 'Parameter load failed'))
        return
      rows = r['parameters']
    for p in rows:
      row = FlowPanel(spacing='small')
      row.add_component(Label(text=p['param_code'], bold=True, width=220))
      tb = TextBox(text=p['param_value'], width=110)
      row.add_component(tb)
      btn = Button(text='Save', role='outlined-button')

      def make_save(code, box):
        def _save(**e):
          res = anvil.server.call('ship_plan_set_parameter', code,
                                  box.text or '')
          if res.get('status') != 'success':
            alert(res.get('message', 'Save failed'))
          else:
            Notification(f'{code} saved', timeout=2).show()
        return _save

      btn.set_event_handler('click', make_save(p['param_code'], tb))
      row.add_component(btn)
      row.add_component(Label(text=p['description'], italic=True,
                              foreground='#898781'))
      self.pnl_params.add_component(row)

  # =================================================================
  # 2. Special customers
  # =================================================================
  def _load_customers(self, rows=None):
    self.pnl_customers.clear()
    if rows is None:                 # section-only refresh after a save
      r = anvil.server.call('ship_plan_get_special_customers')
      if r.get('status') != 'success':
        alert(r.get('message', 'Customer load failed'))
        return
      rows = r['customers']
    for c in rows:
      self.pnl_customers.add_component(self._customer_row(c))
    self.pnl_customers.add_component(self._customer_row(None))  # blank = add
    self.pnl_customers.add_component(Label(
      text=('Code is a PREFIX matched against the order ship-to '
            '(e.g. SPACEX catches every SpaceX ship-to).'),
      italic=True, foreground='#898781'))

  def _customer_row(self, c):
    row = FlowPanel(spacing='small')
    tb_code = TextBox(text=(c['customer_code'] if c else ''),
                      placeholder='CODE (prefix)', width=140)
    tb_code.enabled = c is None            # code is the PK — new rows only
    tb_name = TextBox(text=(c['customer_name'] if c else ''),
                      placeholder='name', width=200)
    tb_val = TextBox(text=(str(c['surcharge_value']) if c else '1.5'),
                     width=70)
    cb_active = CheckBox(text='active',
                         checked=(bool(c['is_active']) if c else True))
    btn = Button(text='Save' if c else 'Add', role='outlined-button')

    def _save(**e):
      try:
        val = float(tb_val.text)
      except (TypeError, ValueError):
        alert('Surcharge value must be a number (multiplier, e.g. 1.5).')
        return
      res = anvil.server.call('ship_plan_upsert_special_customer',
                              tb_code.text, tb_name.text, val,
                              cb_active.checked)
      if res.get('status') != 'success':
        alert(res.get('message', 'Save failed'))
      else:
        Notification('Saved', timeout=2).show()
        self._load_customers()

    btn.set_event_handler('click', _save)
    for comp in (tb_code, tb_name, Label(text='×'), tb_val, cb_active, btn):
      row.add_component(comp)
    return row

  # =================================================================
  # 3. Day overrides
  # =================================================================
  def _load_overrides(self, rows=None):
    self.pnl_overrides.clear()
    if rows is None:                 # section-only refresh after a save
      r = anvil.server.call('ship_plan_get_overrides', self.override_date)
      if r.get('status') != 'success':
        alert(r.get('message', 'Override load failed'))
        return
      rows = r['overrides']
    for o in rows:
      self.pnl_overrides.add_component(self._override_row(o))
    self.pnl_overrides.add_component(Label(
      text=('"(snapshot)" = the 08:45 Paychex feed decides. An override '
            'out-votes it for this day only. Trainee pre-fills the '
            'TRAINEE_MULTIPLIER parameter; edit the factor per person if '
            'needed. Auto-marking arrives with Paychex em_type.'),
      italic=True, foreground='#898781'))

  def _override_row(self, o):
    row = FlowPanel(spacing='small')
    snap = o.get('snapshot_status') or 'no record'
    row.add_component(Label(text=o['full_name'], bold=True, width=190))
    row.add_component(Label(text=f"shift {o.get('shift', 1)} · {snap}",
                            foreground='#898781', width=160))

    dd = DropDown(items=self.OVERRIDE_TYPES, width=130)
    dd.selected_value = o.get('override_type') or '(snapshot)'
    tb_factor = TextBox(width=60)
    if o.get('capacity_factor') is not None:
      tb_factor.text = f"{float(o['capacity_factor']):g}"
    btn = Button(text='Save', role='outlined-button')

    def _save(**e):
      otype = dd.selected_value
      factor = None
      if tb_factor.text:
        try:
          factor = float(tb_factor.text)
        except ValueError:
          alert('Factor must be a number between 0 and 1.')
          return
      res = anvil.server.call(
        'ship_plan_set_override', self.override_date, o['employee_id'],
        None if otype == '(snapshot)' else otype, factor)
      if res.get('status') != 'success':
        alert(res.get('message', 'Save failed'))
      else:
        Notification(f"{o['full_name']} saved", timeout=2).show()
        self._load_overrides()

    btn.set_event_handler('click', _save)
    for comp in (dd, Label(text='factor'), tb_factor, btn):
      row.add_component(comp)
    return row