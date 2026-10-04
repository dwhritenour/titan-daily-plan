from ._anvil_designer import RetroReviewRowTemplate
from anvil import *
import anvil.server


class RetroReviewRow(RetroReviewRowTemplate):
  """
    One answered question for Dave to confirm. Designer components:
      lbl_head      date · order / SKU · case · side (bold)
      lbl_question  the question
      lbl_answer    "Answer (Tara, 09/09 08:12): …"
      tb_reading    TextBox — the agent's reading, editable
      dd_evidence   DropDown — Verified / Probable / Unverified / Disputed / Unknown
      dd_cause      DropDown — cause keys (label shown, key stored)
      dd_resolution DropDown — EXPLAINED / EXCEPTION / RULE_CANDIDATE / CLOSED_UNKNOWN / DISPUTED
      btn_confirm   'Confirm reading'
      lbl_status    small status line (optional)
  """

  _EVIDENCE = ['Verified', 'Probable', 'Unverified', 'Disputed', 'Unknown']
  _RESOLUTION = ['EXPLAINED', 'EXCEPTION', 'RULE_CANDIDATE', 'CLOSED_UNKNOWN', 'DISPUTED']

  def __init__(self, **properties):
    self.init_components(**properties)
    self.btn_confirm.set_event_handler('click', self._confirm)
    self._paint()

  def _paint(self):
    q = self.item or {}
    side = 'Sales' if q.get('routed_to') == 'SALES' else 'Production Control'
    self.lbl_head.text = (f"{q.get('plan_date')}  ·  {q.get('nx_order_numb')}{('-' + (q.get('nx_rel_numb') or '') + '-' + q['nx_item_no']) if q.get('nx_item_no') else ''} / {q.get('nx_sku')}"
                          f"  ·  {q.get('case_text', '')}  ·  {side}")
    self.lbl_question.text = q.get('question_text') or ''
    self.lbl_answer.text = (f"Answer ({q.get('answered_by') or '—'}, "
                            f"{(q.get('answered_at') or '')[:16]}): {q.get('answer_text') or ''}")

    pending = not q.get('proposed_reading')
    self.tb_reading.text = q.get('proposed_reading') or ''
    self.tb_reading.placeholder = ('Agent reading pending — refresh in a minute'
                                   if pending else 'Reading')
    self.dd_evidence.items = self._EVIDENCE
    self.dd_evidence.selected_value = q.get('proposed_evidence_class') or 'Probable'
    causes = q.get('causes') or []          # 10-04: carried on the item (see retro_review.load)
    if not causes:
      try:
        causes = get_open_form().causes()  # Refresh path, or an older parent
      except Exception:
        pass
    self.dd_cause.items = [(f"{c['cause_key']} — {c['label']}", c['cause_key']) for c in causes]
    keys = [c['cause_key'] for c in causes]
    proposed = q.get('proposed_cause_key')
    # the agent's proposed key when it is a known key; else the first key
    self.dd_cause.selected_value = proposed if proposed in keys else (keys[0] if keys else None)
    self.dd_resolution.items = self._RESOLUTION
    self.dd_resolution.selected_value = 'EXPLAINED'
    self.btn_confirm.text = 'Confirm reading'
    self.btn_confirm.enabled = not pending
    if hasattr(self, 'lbl_status'):
      self.lbl_status.text = ''

  def _confirm(self, **event_args):
    q = self.item or {}
    pin = ''
    try:
      pin = get_open_form().pin()
    except Exception:
      pass
    res = anvil.server.call('retro_confirm', q['variance_id'], self.tb_reading.text,
                            self.dd_evidence.selected_value, self.dd_cause.selected_value,
                            self.dd_resolution.selected_value, pin)
    if res.get('status') != 'success':
      alert(res.get('message', 'Confirm failed'))
      return
    for c in (self.tb_reading, self.dd_evidence, self.dd_cause, self.dd_resolution, self.btn_confirm):
      c.enabled = False
    self.btn_confirm.text = 'Confirmed'
    status_txt = 'Confirmed.' + (f"  ({res['warning']})" if res.get('warning') else '')
    # 071: a confirmed material_short reading placed (or extended) a hold
    h = res.get('hold')
    if h:
      what = 'placed' if h.get('created') else 'extended'
      txt = f"Material hold {what} on {h.get('line')} until {h.get('expires_on')}"
      if h.get('n_unplaced'):
        txt += f"; {h['n_unplaced']} plan line(s) unplaced"
      status_txt += '  ' + txt
      Notification(txt, timeout=6).show()
    if hasattr(self, 'lbl_status'):
      self.lbl_status.text = status_txt