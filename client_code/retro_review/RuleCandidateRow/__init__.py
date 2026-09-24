from ._anvil_designer import RuleCandidateRowTemplate
from anvil import *
import anvil.server
from datetime import datetime


class RuleCandidateRow(RuleCandidateRowTemplate):
  """
    One row of the rules register (dbo.pc_plan_rule_register) awaiting
    Dave's decision. The Monday scan writes PROPOSED rows; this is the
    only place in the app that decides one.

    Designer components:
      lbl_title   the candidate's title (bold)
      lbl_meta    kind · cause · proposed date · status (small, grey)
      lbl_effect  test_summary — the DRAFTED EFFECT, i.e. what approving
                  says yes to (italic)
      lk_why      toggles lbl_desc
      lbl_desc    description — the evidence behind the candidate
                  (collapsed by default)
      tb_note     decision note, stored on the row either way
      btn_approve / btn_reject / btn_build  the three decisions

    Approving RECORDS the decision. The effect itself — a parameter, a
    flag, a code change — is applied separately and should be named in
    the note (rule_decide docstring). NEEDS_BUILD is for a candidate
    whose effect is code nobody has written yet.

    The PIN is the form's single tb_pin at the top (same one confirm
    uses), read through get_open_form().pin().
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    self.btn_approve.set_event_handler('click', lambda **e: self._decide('APPROVED', **e))
    self.btn_reject.set_event_handler('click', lambda **e: self._decide('REJECTED', **e))
    self.btn_build.set_event_handler('click', lambda **e: self._decide('NEEDS_BUILD', **e))
    self.lk_why.set_event_handler('click', self._toggle_why)
    self._paint()

  @staticmethod
  def _day(iso):
    """'2026-09-22T06:00:00' -> 'Tue 09/22'. Server dates arrive as ISO
    strings (make_serializable); anything else is shown as it came."""
    try:
      return datetime.strptime((iso or '')[:10], '%Y-%m-%d').strftime('%a %m/%d')
    except ValueError:
      return iso or '—'

  def _paint(self):
    r = self.item or {}
    self.lbl_title.text = r.get('title') or f"Rule #{r.get('rule_id')}"
    self.lbl_meta.text = (f"{r.get('rule_kind') or '—'}  ·  {r.get('cause_key') or '—'}  ·  "
                          f"proposed {self._day(r.get('proposed_at'))}  ·  "
                          f"status {r.get('status')}")
    self.lbl_effect.text = r.get('test_summary') or 'No drafted effect recorded.'
    self.lbl_desc.text = r.get('description') or ''
    self.lbl_desc.visible = False
    self.lk_why.text = 'why'
    self.lk_why.visible = bool(r.get('description'))
    self.tb_note.text = r.get('decision_note') or ''

  def _toggle_why(self, **event_args):
    self.lbl_desc.visible = not self.lbl_desc.visible
    self.lk_why.text = 'hide why' if self.lbl_desc.visible else 'why'

  def _decide(self, status, **event_args):
    res = anvil.server.call('rule_decide', self.item['rule_id'], status,
                            self.tb_note.text, get_open_form().pin())
    if res.get('status') != 'success':
      alert(res.get('message', 'Decision failed'))
      return
    Notification(f"Rule #{self.item['rule_id']} → {res['new_status']}", timeout=4).show()
    self.parent.raise_event('x-refresh')
