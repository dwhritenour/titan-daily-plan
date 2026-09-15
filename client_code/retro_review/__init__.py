from ._anvil_designer import retro_reviewTemplate
from anvil import *
import anvil.server


class retro_review(retro_reviewTemplate):
  """
    Retro review — Dave confirms the agent's reading of each answer.
    Reached from the completion text (<app-url>#retrev). Everything
    answered-but-unconfirmed, across all days, in one list; the PIN is
    typed once at the top and sent with every Confirm.

    Designer components (spec: documents/Retro_Forms_Specification.md):
      header:  lk_plan ('← Daily Plan'), lbl_title, lbl_summary, btn_refresh,
               tb_pin (TextBox, hide_text=True, placeholder "PIN")
      grid:    rp_queue (RepeatingPanel, item_template RetroReviewRow)
      panels:  ta_days (TextArea read-only: which days still owe answers),
               ta_recent (TextArea read-only: last confirmed readings),
               ta_causes (TextArea read-only: 14-day cause counts + rule candidates)
  """

  _CASE_TXT = {'UNTOUCHED': 'no recorded activity',
               'REMOVED_NO_REASON': 'removed, no reason recorded',
               'REMOVED_WITH_REASON': 'removed with reason',
               'UNPLANNED_SHIP': 'shipped, not on the plan',
               'DATE_MOVED': 'ship-by moved', 'SHIPPED': 'shipped',
               'WORKED': 'worked', 'UNTOUCHED_STRETCH': 'stretch, not reached'}

  def __init__(self, **properties):
    self.init_components(**properties)
    self.data = {}
    self.lk_plan.set_event_handler('click', self._go_plan)
    self.btn_refresh.set_event_handler('click', lambda **e: self.load())
    self.tb_pin.placeholder = 'PIN'
    self.load()

  def _go_plan(self, **event_args):
    set_url_hash('')
    open_form('ship_plan')

  def pin(self):
    return (self.tb_pin.text or '').strip()

  def causes(self):
    return self.data.get('causes') or []

  def load(self):
    r = anvil.server.call('retro_get_review')
    if r.get('status') != 'success':
      alert(r.get('message', 'Review load failed'))
      return
    self.data = r
    queue = r.get('queue') or []
    for q in queue:
      q['case_text'] = self._CASE_TXT.get(q.get('variance_case'), q.get('variance_case'))
    self.lbl_title.text = 'Retro review'
    self.lbl_summary.text = (f"{len(queue)} answer(s) awaiting your reading"
                             if queue else 'Nothing awaiting you.')
    self.rp_queue.items = queue

    if hasattr(self, 'ta_days'):
      days = r.get('days') or []
      self.ta_days.text = '\n'.join(
        f"{d['plan_date']}: {d['n_asked']} asked · {d['n_unanswered']} unanswered · "
        f"{d['n_awaiting_dave']} awaiting you" for d in days) or 'No retro days yet.'
    if hasattr(self, 'ta_recent'):
      rec = r.get('recent') or []
      self.ta_recent.text = '\n'.join(
        f"{c['plan_date']}  {c['nx_order_numb']}{('-' + (c.get('nx_rel_numb') or '') + '-' + c['nx_item_no']) if c.get('nx_item_no') else ''} / {c['nx_sku']}  [{c.get('routed_to')}]  "
        f"{c.get('confirmed_reading')}  — {c.get('evidence_class')} · {c.get('cause_key')} · "
        f"{c.get('resolution')}" for c in rec) or 'No confirmed readings yet.'
    if hasattr(self, 'ta_causes'):
      wk = r.get('week_causes') or []
      rules = r.get('rule_candidates') or []
      lines = [f"{c.get('cause_key')} × {c['n']}" + ('   ● rule candidate' if int(c['n']) >= 3 else '')
               for c in wk] or ['No confirmed causes in the last 14 days.']
      if rules:
        lines.append('')
        lines.append('Rule candidates awaiting you:')
        lines += [f"  #{x['rule_id']}  {x['title']}  [{x['status']}]" for x in rules]
      self.ta_causes.text = '\n'.join(lines)