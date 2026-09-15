from ._anvil_designer import RetroQuestionRowTemplate
from anvil import *
import anvil.server


class RetroQuestionRow(RetroQuestionRowTemplate):
  """
    One retro question. Designer components:
      lbl_head      side + order / SKU + case (bold)
      lbl_question  the question text
      lbl_context   what the records said (grey, small)
      lbl_answer    the answer + readings once given (grey)
      ta_answer     TextArea for the answer (hidden once answered)
      btn_submit    'Submit answer' (hidden once answered)
      btn_ask       'Ask anyway' — only on not-asked rows, PC only (optional)
      lbl_status    small status line under the buttons (optional)
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    self.btn_submit.set_event_handler('click', self._submit)
    if hasattr(self, 'btn_ask'):
      self.btn_ask.set_event_handler('click', self._ask_anyway)
    self._paint()

  def _paint(self):
    v = self.item or {}
    to = v.get('routed_to') or 'PC'
    side = 'Sales' if to == 'SALES' else 'Production Control'
    asked = bool(v.get('asked_at'))
    answered = bool(v.get('answer_text'))

    head = (f"[{side}]  {v.get('nx_order_numb')}{('-' + (v.get('nx_rel_numb') or '') + '-' + v['nx_item_no']) if v.get('nx_item_no') else ''}"
            f" / {v.get('nx_sku')}  ·  {v.get('case_text', '')}")
    if not asked:
      head += f"   —  not asked today (rank {v.get('question_rank')})"
    self.lbl_head.text = head
    self.lbl_head.foreground = '#1b5e20' if to == 'SALES' else '#0d47a1'
    self.lbl_question.text = v.get('question_text') or ''
    self.lbl_context.text = v.get('system_explanation') or ''
    self.lbl_context.visible = bool(self.lbl_context.text)

    # answer / reading state
    lines = []
    if answered:
      lines.append(f"Answer ({v.get('answered_by') or '—'}, "
                   f"{(v.get('answered_at') or '')[:16]}): {v['answer_text']}")
      if v.get('reading_confirmed_at'):
        lines.append(f"Dave's reading: {v.get('confirmed_reading')}  "
                     f"[{v.get('evidence_class')} · {v.get('cause_key')} · {v.get('resolution')}]")
      elif v.get('proposed_reading'):
        lines.append(f"Agent's reading (awaiting Dave): {v['proposed_reading']}  "
                     f"[{v.get('proposed_evidence_class')} · {v.get('proposed_cause_key')}]")
      else:
        lines.append('Reading pending — the agent reads new answers every couple of minutes.')
    elif v.get('resolution') == 'CLOSED_UNKNOWN':
      lines.append('Closed as Unknown — no answer in time.')
    self.lbl_answer.text = '\n'.join(lines)
    self.lbl_answer.visible = bool(lines)

    can_answer = asked and not answered and v.get('resolution') == 'OPEN'
    self.ta_answer.visible = can_answer
    self.btn_submit.visible = can_answer
    self.btn_submit.text = 'Submit answer'
    if hasattr(self, 'btn_ask'):
      self.btn_ask.visible = (not asked) and bool(v.get('is_pc_user'))
      self.btn_ask.text = 'Ask anyway'
    if hasattr(self, 'lbl_status'):
      self.lbl_status.text = ''

  def _submit(self, **event_args):
    v = self.item or {}
    text = (self.ta_answer.text or '').strip()
    if not text:
      alert('Type what happened first — plain words are fine.')
      return
    who = None
    try:
      who = get_open_form().current_who()
    except Exception:
      pass
    res = anvil.server.call('retro_answer', v['variance_id'], text, who)
    if res.get('status') != 'success':
      alert(res.get('message', 'Could not save the answer'))
      return
    v['answer_text'] = text
    v['answered_by'] = who or 'Other'
    v['answered_at'] = ''
    self._paint()
    if hasattr(self, 'lbl_status'):
      self.lbl_status.text = 'Saved. The agent will read it in a minute or two.'

  def _ask_anyway(self, **event_args):
    v = self.item or {}
    res = anvil.server.call('retro_ask_anyway', v['variance_id'])
    if res.get('status') != 'success':
      alert(res.get('message', 'Could not ask'))
      return
    v['asked_at'] = 'now'
    v['asked_to'] = ''
    self._paint()