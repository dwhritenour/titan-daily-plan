from ._anvil_designer import retroTemplate
from anvil import *
import anvil.server


class retro(retroTemplate):
  """
    Retro — the previous-day review for Production Control and Sales.
    Reached from the morning text (<app-url>#retro) or the Retro link on
    the plan. Designer-native: this code binds by name to the components
    below and computes nothing — the server decides what was asked.

    Designer components (spec: documents/Retro_Forms_Specification.md):
      header:   lk_plan ('← Daily Plan'), lbl_title, dd_day, btn_refresh,
                lk_run ('run retro now' — PC only; optional)
      summary:  lbl_summary, lbl_open, lk_narrative, ta_narrative
                (TextArea, read-only, hidden until tapped)
      answer:   dd_who ('I am:'), dd_side (All / Production Control / Sales)
      grid:     rp_questions (RepeatingPanel, item_template RetroQuestionRow)
      closed:   lk_closed, ta_closed (TextArea, read-only, hidden) — optional
  """

  _CASE_TXT = {'UNTOUCHED': 'no recorded activity',
               'REMOVED_NO_REASON': 'removed, no reason recorded',
               'REMOVED_WITH_REASON': 'removed with reason',
               'UNPLANNED_SHIP': 'shipped, not on the plan',
               'DATE_MOVED': 'ship-by moved', 'SHIPPED': 'shipped',
               'WORKED': 'worked', 'UNTOUCHED_STRETCH': 'stretch, not reached',
               'CANCELLED': 'cancelled after publish', 'QTY_CHANGED': 'quantity changed'}
  _SIDES = ['All', 'Production Control', 'Sales']

  def __init__(self, plan_date=None, **properties):
    self.init_components(**properties)
    self.plan_date = plan_date
    self.data = {}
    self.is_pc = True                      # role gate hook (same TODO as ship_plan)

    self.lk_plan.set_event_handler('click', self._go_plan)
    self.dd_day.set_event_handler('change', self._day_changed)
    self.btn_refresh.set_event_handler('click', lambda **e: self.load())
    self.dd_side.items = self._SIDES
    self.dd_side.selected_value = 'All'
    self.dd_side.set_event_handler('change', lambda **e: self._render_rows())
    self.dd_who.set_event_handler('change', self._who_changed)
    self.lk_narrative.set_event_handler('click', self._toggle_narrative)
    if hasattr(self, 'lk_closed'):
      self.lk_closed.set_event_handler('click', self._toggle_closed)
    if hasattr(self, 'lk_run'):
      self.lk_run.text = 'Run Retro Now'
      self.lk_run.visible = self.is_pc
      self.lk_run.set_event_handler('click', self._run_now)
    self.load()

  # ---------------------------------------------------------------- nav
  def _go_plan(self, **event_args):
    set_url_hash('')
    open_form('ship_plan')

  # --------------------------------------------------------------- load
  def load(self):
    r = anvil.server.call('retro_get_day', self.plan_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'Retro load failed'))
      return
    self.data = r
    self.plan_date = r['plan_date']

    # day picker: every reviewed day, newest first, with open-question counts
    days = r.get('days') or []
    items = [(f"{d['plan_date']}" + (f"  · {d['n_open']} open" if d['n_open'] else ''),
              d['plan_date']) for d in days]
    if self.plan_date not in [d['plan_date'] for d in days]:
      items.insert(0, (self.plan_date, self.plan_date))
    self.dd_day.items = items
    self.dd_day.selected_value = self.plan_date

    # who is answering — remembered per browser session
    people = r.get('answerers') or {}
    names = list(people.get('PC') or []) + list(people.get('SALES') or []) + ['Dave', 'Other']
    keep = self.dd_who.selected_value
    self.dd_who.items = names
    self.dd_who.selected_value = keep if keep in names else names[0]

    hdr = r.get('header')
    self.lbl_title.text = f"Retro — {self.plan_date}"
    if not hdr:
      self.lbl_summary.text = ('No retro for this day — it runs each weekday morning '
                               'for the previous published day.')
      self.lbl_open.text = ''
      self.lk_narrative.visible = False
      self.ta_narrative.visible = False
      self.rp_questions.items = []
      return

    moved = int(hdr.get('n_shipped') or 0) + int(hdr.get('n_worked') or 0)
    line = (f"{moved} of {hdr.get('n_planned', 0)} planned lines moved "
            f"({float(hdr.get('moved_inches') or 0):,.0f} of "
            f"{float(hdr.get('planned_inches') or 0):,.0f} in)")
    if hdr.get('reserve_used_pct') is not None:
      line += f" · rush reserve used {float(hdr['reserve_used_pct']):.0f}%"
    self.lbl_summary.text = line
    n_open = r.get('n_open') or {}
    self.lbl_open.text = (f"Open: {n_open.get('PC', 0)} for Production Control · "
                          f"{n_open.get('SALES', 0)} for Sales · "
                          f"{hdr.get('n_questions_asked', 0)} asked today")

    narr = (hdr.get('narrative') or '').strip()
    self.lk_narrative.visible = bool(narr)
    self.lk_narrative.text = 'Show narrative'
    self.ta_narrative.text = narr
    self.ta_narrative.visible = False

    if hasattr(self, 'ta_closed'):
      closed = [v for v in (r.get('variances') or []) if not v.get('question_text')]
      self.ta_closed.text = '\n'.join(
        f"{v['nx_order_numb']}{('-' + (v.get('nx_rel_numb') or '') + '-' + v['nx_item_no']) if v.get('nx_item_no') else ''} / {v['nx_sku']} — "
        f"{self._CASE_TXT.get(v.get('variance_case'), v.get('variance_case'))}: "
        f"{v.get('system_explanation') or ''}" for v in closed)
      self.ta_closed.visible = False
      if hasattr(self, 'lk_closed'):
        self.lk_closed.visible = bool(closed)
        self.lk_closed.text = f"Show the {len(closed)} lines that need no question"
    self._render_rows()

  def _render_rows(self):
    rows = [v for v in (self.data.get('variances') or []) if v.get('question_text')]
    side = self.dd_side.selected_value
    if side == 'Production Control':
      rows = [v for v in rows if v.get('routed_to') != 'SALES']
    elif side == 'Sales':
      rows = [v for v in rows if v.get('routed_to') == 'SALES']
    # asked first (unanswered before answered), then not-asked by rank
    def _key(v):
      asked = 1 if v.get('asked_at') else 0
      answered = 1 if v.get('answer_text') else 0
      return (-asked, answered, v.get('question_rank') or 999)
    rows.sort(key=_key)
    for v in rows:
      v['case_text'] = self._CASE_TXT.get(v.get('variance_case'), v.get('variance_case'))
      v['is_pc_user'] = self.is_pc
    self.rp_questions.items = rows

  # ------------------------------------------------------------- events
  def _day_changed(self, **event_args):
    self.plan_date = self.dd_day.selected_value
    self.load()

  def _who_changed(self, **event_args):
    pass                                   # the row reads current_who() at submit

  def current_who(self):
    w = self.dd_who.selected_value
    return None if w == 'Other' else w

  def _toggle_narrative(self, **event_args):
    self.ta_narrative.visible = not self.ta_narrative.visible
    self.lk_narrative.text = 'Hide narrative' if self.ta_narrative.visible else 'Show narrative'

  def _toggle_closed(self, **event_args):
    if hasattr(self, 'ta_closed'):
      self.ta_closed.visible = not self.ta_closed.visible

  def _run_now(self, **event_args):
    if not confirm(f'Run the retro for {self.plan_date} now? Unanswered questions for '
                   'that day are rebuilt; answers already given are kept.'):
      return
    res = anvil.server.call('retro_run', self.plan_date, True)
    if res.get('status') != 'success':
      alert(res.get('message', 'Retro failed'))
    else:
      c = res.get('counts') or {}
      alert(f"Retro done: {res['n_questions']} question(s) of {res['n_askable']} askable; "
            f"{c.get('SHIPPED', 0)} shipped, {c.get('WORKED', 0)} worked, "
            f"{c.get('UNTOUCHED', 0)} no activity. The narrative and the texts come "
            "from the service run.")
    self.load()