from ._anvil_designer import multiday_orderTemplate
from anvil import *
import anvil.server


# =============================================================================
# multiday_order - the Multi-day Order page (v1.7.1, step 3 of the multi-day
# build; concept daily-plan-multiday-order-page-concept.md, decisions of
# 2026-10-27).
#
# One page per multi-day order: <app-url>#md=<order>, the ⧗ link on a board
# line or a candidate row. Lines down the side, days across the top; each
# cell is one line on one day - planned, counted, or projected ("est.").
# The page owns the ORDER-level decisions (assemblers per day, a share for
# one line on one day, the partials rule, pause / resume, built by PC's
# word); the star on the board keeps the one-line, one-day edits (tier,
# reason, note, today's quantity).
#
# The client computes nothing: the grid (markdown), the day list, the
# projection and the labels all come from ship_plan_multiday_get. Writes:
#   ship_plan_multiday_plan_set   assemblers per day, share per line-day,
#                                 done (pc_multiday_plan, migration 121)
#   ship_plan_set_qty             a share for a line that is ON THE BOARD
#                                 that day (same path as the star)
#   ship_plan_multiday_set        partials rule / pause (the notes, Snoozed)
#   ship_plan_multiday_resume     end a pause early
#
# Designer components (custom-HTML form; hasattr-guarded like the others):
#   lk_plan, lbl_title, btn_refresh, lbl_sub, lbl_paused
#   rt_grid (RichText, markdown), lbl_grid_note
#   dd_day, tb_assemblers, btn_save_day, lbl_day_hint
#   dd_line, dd_share_day, tb_share, btn_save_share, lbl_share_hint
#   dd_partials, btn_save_partials, lbl_partials_hint
#   tb_pause_to, tb_pause_why, btn_pause, btn_resume, lbl_pause_hint
#   cb_done, lbl_done_hint
#   rt_help (RichText, markdown)
# =============================================================================

HELP = """
**How to read the grid.** Past days show what was planned and what the assembly tablet counted. Today shows what is on the board now. Days marked *est.* are what the 06:30 run would propose given the assemblers planned - nothing there is a commitment; the page is redrawn every morning. *left* is pieces still to build: ordered minus the tablet count when there is one, otherwise minus the shares already planned (an estimate).

**Assemblers planned** is PC's number for a day. The share for that day becomes assemblers x one assembler-day (capacity / crew that morning), never more than what is left of the multi-day pool. Blank = the pool rule: the order takes the whole pool, as before. **Assemblers assigned** comes from the Supervisor board and is shown, never changed here.

**Pieces of a line on a day** beats the computed share for that one cell. On a day that is already on the board it changes the board line (same as the star); on a later day it is kept for the 06:30 run. Blank = computed.

**Partials** and **Pause** apply to every line of the order on the board. A paused order leaves the plan until the date and comes back first that morning; Resume ends it early (it is proposed first on the next run). **Built (PC's word)** tells the engine and the retro the order is done when the tablet has not said so.
"""


class multiday_order(multiday_orderTemplate):
  def __init__(self, order=None, **properties):
    self.init_components(**properties)
    self.is_pc = True                      # same gate as the board (TODO: real role source)
    if not order:
      h = get_url_hash()
      if isinstance(h, str) and h.startswith('md='):
        order = h[3:].strip()
      elif isinstance(h, dict):
        order = h.get('md')
    self.order = (order or '').strip()
    self.data = None
    if hasattr(self, 'lk_plan'):
      self.lk_plan.set_event_handler('click', self._go_plan)
    if hasattr(self, 'btn_refresh'):
      self.btn_refresh.set_event_handler('click', lambda **e: self.load())
    if hasattr(self, 'btn_save_day'):
      self.btn_save_day.set_event_handler('click', self._save_day)
    if hasattr(self, 'tb_assemblers'):
      self.tb_assemblers.set_event_handler('change', self._asm_hint)
      self.tb_assemblers.set_event_handler('pressed_enter', self._save_day)
    if hasattr(self, 'btn_save_share'):
      self.btn_save_share.set_event_handler('click', self._save_share)
    if hasattr(self, 'tb_share'):
      self.tb_share.set_event_handler('pressed_enter', self._save_share)
    if hasattr(self, 'btn_save_partials'):
      self.btn_save_partials.set_event_handler('click', self._save_partials)
    if hasattr(self, 'btn_pause'):
      self.btn_pause.set_event_handler('click', self._pause)
    if hasattr(self, 'btn_resume'):
      self.btn_resume.set_event_handler('click', self._resume)
    if hasattr(self, 'cb_done'):
      self.cb_done.set_event_handler('change', self._done_change)
    if hasattr(self, 'dd_partials'):
      self.dd_partials.items = [('Not decided yet', 'DECIDE'),
                                ('Ship them that day', 'OK'),
                                ('Hold until the order is complete', 'COMPLETE')]
    if hasattr(self, 'rt_help'):
      self.rt_help.format = 'markdown'
      self.rt_help.content = HELP
    if not self.order:
      if hasattr(self, 'lbl_title'):
        self.lbl_title.text = 'Multi-day order - which one?'
      if hasattr(self, 'lbl_sub'):
        self.lbl_sub.text = 'Open this page from the ⧗ link on a board line or a candidate row (#md=<order>).'
      return
    self.load()

  # ------------------------------------------------------------------
  def _go_plan(self, **event_args):
    set_url_hash('')
    open_form('ship_plan')

  def load(self):
    r = anvil.server.call('ship_plan_multiday_get', self.order)
    if r.get('status') != 'success':
      if hasattr(self, 'lbl_title'):
        self.lbl_title.text = f'Multi-day order {self.order}'
      if hasattr(self, 'lbl_sub'):
        self.lbl_sub.text = r.get('message') or 'Could not load the order.'
      if hasattr(self, 'oc_controls'):
        self.oc_controls.visible = False
      return
    self.data = r
    self._render()

  # ------------------------------------------------------------------
  @staticmethod
  def _mdy(d):
    d = str(d or '')[:10]
    return f"{int(d[5:7])}/{int(d[8:10])}" if len(d) == 10 else d

  def _board_lines(self, day):
    """Active plan rows of the order on a day: {(rel, item): row}."""
    out = {}
    for pr in (self.data.get('plan_rows') or []):
      if str(pr['plan_date'])[:10] == day and pr['line_status'] in ('Planned', 'Proposed', 'Confirmed'):
        out[(pr['nx_rel_numb'], str(pr['nx_item_no']))] = pr
    return out

  def _render(self):
    d = self.data
    today = d.get('today') or ''
    ship_by = str(d.get('ship_by') or '')[:10]
    late = bool(ship_by) and ship_by < today
    if hasattr(self, 'lbl_title'):
      self.lbl_title.text = (f"Multi-day order {d['order']}"
                             + (f" · {d['customer']}" if d.get('customer') else '')
                             + (f" · ship-by {self._mdy(ship_by)}" + (' (late)' if late else '') if ship_by else ''))
    lines = d.get('lines') or []
    tot_in = sum(float(l.get('inches') or 0) for l in lines)
    tot_pc = sum(float(l.get('qty') or 0) for l in lines)
    left_in = float(d.get('remaining_inches') or 0)
    left_pc = sum(float(l.get('remaining') or 0) for l in lines)
    proj = d.get('projection') or []
    pool = float(d.get('pool_inches') or 0)
    asm_day = float(d.get('assembler_day_in') or 0)
    crew = d.get('crew')
    partials = {'OK': 'ship each day', 'COMPLETE': 'hold until complete'}.get(d.get('partials'), 'not decided yet')
    more = (f"about {len(proj)} more day(s) est." if left_in > 0.5 and d.get('days_left_est') is not None
            else ('nothing left to plan' if left_in <= 0.5 else f"{len(proj)}+ more days est."))
    if hasattr(self, 'lbl_sub'):
      self.lbl_sub.text = (f"{tot_in:,.0f} in · {tot_pc:g} pcs ordered · {left_in:,.0f} in / {left_pc:g} pcs left · {more}"
                           f" · pool {pool:,.0f} in/day · one assembler-day ≈ {asm_day:,.0f} in"
                           + (f" (crew {float(crew):g})" if crew else '') + f" · partials: {partials}")
    pz = d.get('paused')
    if hasattr(self, 'lbl_paused'):
      self.lbl_paused.visible = bool(pz)
      if pz:
        self.lbl_paused.text = (f"⏸ PAUSED to {pz.get('to')}" + (f": {pz.get('reason')}" if pz.get('reason') else '')
                                + ' - it comes back first that morning.')
    if hasattr(self, 'rt_grid'):
      self.rt_grid.format = 'markdown'
      self.rt_grid.content = d.get('grid_md') or '(no grid)'
    if hasattr(self, 'lbl_grid_note'):
      self.lbl_grid_note.text = ('est. = what the 06:30 run would propose with the assemblers planned; not a commitment. '
                                 'left = ordered - tablet count (fact) or - shares planned (estimate). '
                                 + ' · '.join(f"-{l['item']}: {l.get('built_basis')}" for l in lines))
    # --- controls ---------------------------------------------------
    if hasattr(self, 'oc_controls'):
      self.oc_controls.visible = self.is_pc
    days = d.get('days') or []
    editable = [x for x in days if x['date'] >= today]
    if not editable:
      editable = [{'date': d.get('projection_start') or today, 'label': d.get('projection_start') or today,
                   'kind': 'est', 'on_board': False}]
    pc_asm = {str(r['plan_date'])[:10]: r['assemblers_planned'] for r in (d.get('pc_rows') or []) if r['nx_rel_numb'] is None}
    items = [(x['label'] + (f" - {float(pc_asm[x['date']]):g} planned" if pc_asm.get(x['date']) is not None else ''), x['date'])
             for x in editable]
    if hasattr(self, 'dd_day'):
      cur = self.dd_day.selected_value
      self.dd_day.items = items
      self.dd_day.selected_value = cur if cur in [v for _, v in items] else (
        next((v for _, v in items if v >= (d.get('projection_start') or today)), items[0][1]))
      self.dd_day.set_event_handler('change', self._day_change)
      self._day_change()
    if hasattr(self, 'dd_line'):
      self.dd_line.items = [(f"-{l['item']} {l['sku']} ({float(l['qty'] or 0):g} pc, {float(l.get('remaining') or 0):g} left)",
                             f"{l['rel']}|{l['item']}") for l in lines]
    if hasattr(self, 'dd_share_day'):
      cur = self.dd_share_day.selected_value
      self.dd_share_day.items = [(x['label'], x['date']) for x in editable]
      self.dd_share_day.selected_value = cur if cur in [x['date'] for x in editable] else editable[0]['date']
      self.dd_share_day.set_event_handler('change', self._share_change)
    if hasattr(self, 'dd_line'):
      self.dd_line.set_event_handler('change', self._share_change)
      self._share_change()
    on_board = bool(self._board_lines(d.get('board_date') or today))
    if hasattr(self, 'dd_partials'):
      self.dd_partials.selected_value = d.get('partials') or 'DECIDE'
      self.dd_partials.enabled = on_board
    if hasattr(self, 'btn_save_partials'):
      self.btn_save_partials.enabled = on_board
    if hasattr(self, 'lbl_partials_hint'):
      self.lbl_partials_hint.text = ('applies to every line of the order on the board' if on_board
                                     else 'the order is not on the board today - partials are set once it is')
    if hasattr(self, 'btn_pause'):
      self.btn_pause.enabled = on_board and not pz
      self.btn_pause.visible = not pz
    if hasattr(self, 'tb_pause_to'):
      self.tb_pause_to.enabled = on_board and not pz
    if hasattr(self, 'tb_pause_why'):
      self.tb_pause_why.enabled = on_board and not pz
    if hasattr(self, 'btn_resume'):
      self.btn_resume.visible = bool(pz)
    if hasattr(self, 'lbl_pause_hint'):
      self.lbl_pause_hint.text = ('paused - Resume brings it back on the next 06:30 run' if pz
                                  else ('the order leaves the plan until that date and comes back first' if on_board
                                        else 'pause applies to an order on the board'))
    done_rows = [r for r in (d.get('pc_rows') or []) if r['nx_rel_numb'] is None and r.get('done_by_pc')]
    tablet_done = all(l.get('done') for l in lines) and lines
    if hasattr(self, 'cb_done'):
      self.cb_done.checked = bool(done_rows) or all(l.get('built_basis') == 'PC' for l in lines)
      self.cb_done.enabled = True
    if hasattr(self, 'lbl_done_hint'):
      self.lbl_done_hint.text = ('the tablet already says built' if tablet_done and not done_rows
                                 else (f"set {str(done_rows[0]['plan_date'])[:10]} by {done_rows[0].get('set_by') or '?'}" if done_rows
                                       else 'the engine rides the order at 0 in and the retro records PC-reported'))

  # ------------------------------------------------------------------
  def _day_change(self, **event_args):
    """Prefill the headcount box with what PC planned for the chosen day."""
    if not (hasattr(self, 'dd_day') and hasattr(self, 'tb_assemblers')):
      return
    day = self.dd_day.selected_value
    row = next((r for r in (self.data.get('pc_rows') or [])
                if r['nx_rel_numb'] is None and str(r['plan_date'])[:10] == day), None)
    v = row['assemblers_planned'] if row else None
    self.tb_assemblers.text = f"{float(v):g}" if v is not None else ''
    self._asm_hint()

  def _asm_hint(self, **event_args):
    """The cost next to the number (concept §4): n assemblers = n x one
    assembler-day = share of the pool, and what is left for other work."""
    if not hasattr(self, 'lbl_day_hint'):
      return
    d = self.data or {}
    pool = float(d.get('pool_inches') or 0)
    asm_day = float(d.get('assembler_day_in') or 0)
    cap = float(d.get('capacity_inches') or 0)
    crew = float(d.get('crew') or 0)
    txt = (self.tb_assemblers.text or '').strip() if hasattr(self, 'tb_assemblers') else ''
    day = self.dd_day.selected_value if hasattr(self, 'dd_day') else ''
    on_board = bool(self._board_lines(day)) if (day and self.data) else False
    if not txt:
      self.lbl_day_hint.text = (f"blank = the pool rule: up to {pool:,.0f} in (the whole multi-day pool)"
                                + (' · this day is already on the board; the headcount counts for a re-run only' if on_board else ''))
      return
    try:
      n = float(txt)
    except Exception:
      self.lbl_day_hint.text = 'a number, please (0 or blank = pool rule)'
      return
    if n <= 0:
      self.lbl_day_hint.text = f"0 = the pool rule: up to {pool:,.0f} in"
      return
    share = n * asm_day
    capped = share > pool + 1e-9
    share_eff = min(share, pool)
    pct = (share_eff / pool * 100) if pool > 0 else 0
    rest_crew = max(0.0, crew - n)
    rest_in = max(0.0, cap - share_eff)
    self.lbl_day_hint.text = (f"{n:g} assembler(s) = {share:,.0f} in"
                              + (f", capped at the pool ({pool:,.0f} in)" if capped else f" = {pct:,.0f}% of the pool")
                              + f" · {rest_crew:g} crew left for {rest_in:,.0f} in of other work"
                              + (' · already on the board; counts for a re-run only' if on_board else ''))

  def _save_day(self, **event_args):
    if not (hasattr(self, 'dd_day') and hasattr(self, 'tb_assemblers')):
      return
    day = self.dd_day.selected_value
    txt = (self.tb_assemblers.text or '').strip()
    if txt:
      try:
        n = float(txt)
      except Exception:
        alert('Assemblers must be a number (blank clears it).')
        return
      if n < 0 or n > 50:
        alert('Assemblers: 0 to 50.')
        return
    r = anvil.server.call('ship_plan_multiday_plan_set', self.order, day, assemblers=(txt if txt else ''))
    if r.get('status') != 'success':
      alert(r.get('message', 'Save failed'))
      return
    a = r.get('assemblers')
    Notification(f"{self.order} on {self._mdy(day)}: {('%g assembler(s) planned' % float(a)) if a is not None else 'pool rule (headcount cleared)'}",
                 timeout=4).show()
    self.load()

  # ------------------------------------------------------------------
  def _share_change(self, **event_args):
    """Prefill the pieces box: the board quantity for a day on the board,
    PC's override for a later day, blank = computed."""
    if not (hasattr(self, 'dd_line') and hasattr(self, 'dd_share_day') and hasattr(self, 'tb_share')):
      return
    key = self.dd_share_day.selected_value
    lv = self.dd_line.selected_value or '|'
    rel, item = lv.split('|', 1)
    bl = self._board_lines(key).get((rel, item))
    if bl:
      self.tb_share.text = f"{float(bl['qty_planned'] or 0):g}"
      if hasattr(self, 'lbl_share_hint'):
        self.lbl_share_hint.text = 'on the board that day - saving changes the board line (same as the star)'
      return
    row = next((r for r in (self.data.get('pc_rows') or [])
                if r['nx_rel_numb'] == rel and str(r['nx_item_no']) == item and str(r['plan_date'])[:10] == key), None)
    v = row['share_pcs'] if row else None
    self.tb_share.text = f"{float(v):g}" if v is not None else ''
    if hasattr(self, 'lbl_share_hint'):
      self.lbl_share_hint.text = ('PC override for that day - the 06:30 run plans this many' if v is not None
                                  else 'blank = computed from the assemblers planned (est. in the grid)')

  def _save_share(self, **event_args):
    if not (hasattr(self, 'dd_line') and hasattr(self, 'dd_share_day') and hasattr(self, 'tb_share')):
      return
    key = self.dd_share_day.selected_value
    lv = self.dd_line.selected_value or ''
    if '|' not in lv:
      alert('Pick a line.')
      return
    rel, item = lv.split('|', 1)
    txt = (self.tb_share.text or '').strip()
    qty = None
    if txt:
      try:
        qty = float(txt)
      except Exception:
        alert('Pieces must be a number (blank = computed).')
        return
      if qty < 0:
        alert('Pieces cannot be negative.')
        return
    line = next((l for l in (self.data.get('lines') or []) if l['rel'] == rel and str(l['item']) == item), None)
    full = float(line['qty'] or 0) if line else 0
    if qty is not None and full and qty > full:
      alert(f"{qty:g} is more than the {full:g} ordered.")
      return
    bl = self._board_lines(key).get((rel, item))
    if bl:
      if qty is None or qty <= 0:
        alert('A line on the board needs a quantity above zero (Remove it from the board instead).')
        return
      r = anvil.server.call('ship_plan_set_qty', key, self.order, rel, item, qty, why='multi-day page')
      if r.get('status') != 'success':
        alert(r.get('message', 'Quantity change failed'))
        return
      Notification(f"Board {self._mdy(key)} -{item}: {r.get('old')} → {r.get('new')}", timeout=4).show()
    else:
      r = anvil.server.call('ship_plan_multiday_plan_set', self.order, key, rel=rel, item=item,
                            share_pcs=(txt if txt else ''))
      if r.get('status') != 'success':
        alert(r.get('message', 'Save failed'))
        return
      s = r.get('share_pcs')
      Notification(f"{self.order}-{item} on {self._mdy(key)}: {('%g pieces' % float(s)) if s is not None else 'computed (override cleared)'}",
                   timeout=4).show()
    self.load()

  # ------------------------------------------------------------------
  def _save_partials(self, **event_args):
    if not hasattr(self, 'dd_partials'):
      return
    mode = self.dd_partials.selected_value or 'DECIDE'
    r = anvil.server.call('ship_plan_multiday_set', self.data.get('board_date'), self.order, partials=mode)
    if r.get('status') != 'success':
      alert(r.get('message', 'Partials update failed'))
      return
    Notification(f"{self.order}: partials = {mode} ({r.get('n_lines')} line(s))", timeout=4).show()
    self.load()

  def _pause(self, **event_args):
    to = (self.tb_pause_to.text or '').strip() if hasattr(self, 'tb_pause_to') else ''
    why = (self.tb_pause_why.text or '').strip() if hasattr(self, 'tb_pause_why') else ''
    if len(to) != 10 or to[4] != '-' or to[7] != '-':
      alert('Pause until: a date as YYYY-MM-DD.')
      return
    if not why:
      alert('A pause needs a reason.')
      return
    if not confirm(f"Pause {self.order} until {to}? Its lines leave today's plan; it comes back first that morning."):
      return
    r = anvil.server.call('ship_plan_multiday_set', self.data.get('board_date'), self.order,
                          pause_to=to, pause_reason=why)
    if r.get('status') != 'success':
      alert(r.get('message', 'Pause failed'))
      return
    Notification(f"{self.order}: paused to {r.get('paused_to')} ({r.get('n_lines')} line(s))", timeout=5).show()
    if hasattr(self, 'tb_pause_to'):
      self.tb_pause_to.text = ''
    if hasattr(self, 'tb_pause_why'):
      self.tb_pause_why.text = ''
    self.load()

  def _resume(self, **event_args):
    pz = (self.data or {}).get('paused') or {}
    tb = TextBox(placeholder='note (optional)')
    if not confirm(f"Resume {self.order} (paused to {pz.get('to') or '?'})? It is proposed first on the next morning run.",
                   content=tb):
      return
    r = anvil.server.call('ship_plan_multiday_resume', self.order, note=(tb.text or '').strip() or None)
    if r.get('status') != 'success':
      alert(r.get('message', 'Resume failed'))
      return
    Notification(r.get('message') or f'{self.order} resumed', timeout=5).show()
    self.load()

  # ------------------------------------------------------------------
  def _done_change(self, **event_args):
    checked = bool(self.cb_done.checked)
    d = self.data or {}
    if checked:
      if not confirm(f"Mark {self.order} built on your word? The engine rides it at 0 in and the retro records it as PC-reported."):
        self.cb_done.checked = False
        return
      r = anvil.server.call('ship_plan_multiday_plan_set', self.order, d.get('board_date') or d.get('today'), done=True)
      if r.get('status') != 'success':
        alert(r.get('message', 'Save failed'))
        self.cb_done.checked = False
        return
      Notification(f"{self.order}: built (PC's word)", timeout=4).show()
    else:
      rows = [r for r in (d.get('pc_rows') or []) if r.get('done_by_pc')]
      for row in rows:
        r = anvil.server.call('ship_plan_multiday_plan_set', self.order, str(row['plan_date'])[:10],
                              rel=row['nx_rel_numb'], item=(str(row['nx_item_no']) if row['nx_rel_numb'] is not None else None),
                              done=False)
        if r.get('status') != 'success':
          alert(r.get('message', 'Save failed'))
          break
      Notification(f"{self.order}: not built after all", timeout=4).show()
    self.load()