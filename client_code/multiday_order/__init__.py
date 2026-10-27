from ._anvil_designer import multiday_orderTemplate
from anvil import *
import anvil.server


# =============================================================================
# multiday_order - the Multi-day WORK ORDER page (v1.9; concept
# daily-plan-multiday-order-page-concept.md §10-11, Dave 2026-10-27 evening:
# one work order per page, the week Mon-Fri across, assemblers typed per day).
#
# One page per WORK ORDER (a sales-order line): <app-url>#md=<order>-<item>,
# the ⧗ link on a board line or a candidate row. The five weekdays are the
# columns (◀ ▶ move the week); the 'assemblers' row is a TextBox per day -
# PC types the number of assemblers on this work order for that day; a blank
# day carries the previous number, 0 = nobody, no number anywhere = the pool
# rule. The 'pieces' row shows what was planned / counted, what is on the
# board today, and what the 06:30 run would propose ("est.") with those
# numbers. The other lines of the same order are links under the header -
# never on the same grid (an assembler works one work order at a time).
#
# PC decides here: the headcount per day, the order's PARTIALS rule, PAUSE /
# RESUME (the order's), and BUILT on PC's word (this work order). Today's
# quantity on the board is still the star's.
#
# The client computes nothing: the grid rows, the day list, the projection,
# the cost sentence and the labels all come from ship_plan_multiday_get.
# Writes:
#   ship_plan_multiday_plan_set   assemblers (line row, that day), done,
#                                 clear_assemblers (back to the pool rule)
#   ship_plan_multiday_set        partials rule / pause (the notes, Snoozed)
#   ship_plan_multiday_resume     end a pause early
#
# Designer components (custom-HTML form; hasattr-guarded like the others):
#   lk_plan, lbl_title, btn_refresh, lbl_sub, lbl_paused
#   flow_siblings: lbl_sib_prompt, lk_sib1..lk_sib3 (the order's other lines)
#   flow_week: lk_prev_week, lbl_week, lk_next_week, lk_this_week
#   dg_grid (DataGrid, columns line + d1..d5; titles set at runtime) > rp_grid
#     (RepeatingPanel, item template MultidayGridRow: lbl_line, lbl_dN + tb_dN)
#   lbl_cost, lbl_grid_note, lk_clear_asm
#   dd_partials, btn_save_partials, lbl_partials_hint
#   tb_pause_to, tb_pause_why, btn_pause, btn_resume, lbl_pause_hint
#   cb_done, lbl_done_hint
#   rt_help (RichText, markdown)
# =============================================================================

HELP = """
**How to read the grid.** The week, Monday to Friday (◀ ▶ for another week). Past days show what was planned and what the assembly tablet counted; today shows what is on the board now; days marked *est.* are what the 06:30 run would propose with the assemblers you typed - nothing there is a commitment, and the page is redrawn every morning. *left* (in the header) is pieces still to build: ordered minus the tablet count when there is one, otherwise minus the shares already planned on this release (an estimate).

**Assemblers** is PC's number for each day, typed in the grid. With a number, the morning run gives this work order that many assembler-days of work (capacity / crew, about 165 in each), never more than what is left of the day's multi-day pool. A blank day carries the previous day's number (shown grey); 0 means nobody on it that day and nothing of it is planned; no number anywhere means the pool rule - the order takes what the pool allows, smallest line first. "Back to the pool rule" forgets every number. The other lines of the order keep the pool rule unless you put people on them too (open them from the links above).

**Partials** and **Pause** are the order's: they apply to every line of it on the board. A paused order leaves the plan until the date and comes back first that morning; Resume ends it early. **Built (PC's word)** tells the engine and the retro this work order is done when the tablet has not said so.
"""


class multiday_order(multiday_orderTemplate):
  def __init__(self, order=None, item=None, **properties):
    self.init_components(**properties)
    self.is_pc = True                      # same gate as the board (TODO: real role source)
    if not order:
      h = get_url_hash()
      if isinstance(h, str) and h.startswith('md='):
        order = h[3:].strip()
      elif isinstance(h, dict):
        order = h.get('md')
    order = (order or '').strip()
    if order and '-' in order and not item:
      order, item = order.split('-', 1)
    self.order = order
    self.item = (str(item).strip() if item not in (None, '') else None)
    self.data = None
    if hasattr(self, 'lk_plan'):
      self.lk_plan.set_event_handler('click', self._go_plan)
    if hasattr(self, 'btn_refresh'):
      self.btn_refresh.set_event_handler('click', lambda **e: self.load())
    self.week_start = None                 # None = this week
    if hasattr(self, 'lk_prev_week'):
      self.lk_prev_week.set_event_handler('click', lambda **e: self._move_week('prev_week'))
    if hasattr(self, 'lk_next_week'):
      self.lk_next_week.set_event_handler('click', lambda **e: self._move_week('next_week'))
    if hasattr(self, 'lk_this_week'):
      self.lk_this_week.set_event_handler('click', lambda **e: self._move_week(None))
    if hasattr(self, 'lk_clear_asm'):
      self.lk_clear_asm.set_event_handler('click', self._clear_asm)
    if hasattr(self, 'btn_save_partials'):
      self.btn_save_partials.set_event_handler('click', self._save_partials)
    if hasattr(self, 'btn_pause'):
      self.btn_pause.set_event_handler('click', self._pause)
    if hasattr(self, 'btn_resume'):
      self.btn_resume.set_event_handler('click', self._resume)
    if hasattr(self, 'cb_done'):
      self.cb_done.set_event_handler('change', self._done_change)
    for n in (1, 2, 3):
      lk = getattr(self, f'lk_sib{n}', None)
      if lk is not None:
        lk.set_event_handler('click', self._open_sibling)
    if hasattr(self, 'dd_partials'):
      self.dd_partials.items = [('Not decided yet', 'DECIDE'),
                                ('Ship them that day', 'OK'),
                                ('Hold until the order is complete', 'COMPLETE')]
    if hasattr(self, 'rt_help'):
      self.rt_help.format = 'markdown'
      self.rt_help.content = HELP
    if not self.order:
      if hasattr(self, 'lbl_title'):
        self.lbl_title.text = 'Multi-day work order - which one?'
      if hasattr(self, 'lbl_sub'):
        self.lbl_sub.text = 'Open this page from the ⧗ link on a board line or a candidate row (#md=<order>-<item>).'
      return
    self.load()

  # ------------------------------------------------------------------
  def _go_plan(self, **event_args):
    set_url_hash('')
    open_form('ship_plan')

  def _open_sibling(self, sender, **event_args):
    it = getattr(sender, 'tag', None)
    if not it:
      return
    self.item = str(it)
    set_url_hash(f'md={self.order}-{self.item}')
    self.load()

  def _move_week(self, which):
    d = self.data or {}
    self.week_start = d.get(which) if which else None
    self.load()

  def load(self):
    r = anvil.server.call('ship_plan_multiday_get', self.order, self.item, week_start=self.week_start)
    if r.get('status') != 'success':
      if hasattr(self, 'lbl_title'):
        self.lbl_title.text = f'Multi-day work order {self.order}' + (f'-{self.item}' if self.item else '')
      if hasattr(self, 'lbl_sub'):
        self.lbl_sub.text = r.get('message') or 'Could not load the work order.'
      if hasattr(self, 'oc_controls'):
        self.oc_controls.visible = False
      return
    self.data = r
    self.item = str((r.get('line') or {}).get('item') or self.item or '')
    self._render()

  # ------------------------------------------------------------------
  @staticmethod
  def _mdy(d):
    d = str(d or '')[:10]
    return f"{int(d[5:7])}/{int(d[8:10])}" if len(d) == 10 else d

  def _on_board_today(self):
    return bool((self.data or {}).get('on_board'))     # the order has an active line on today's board

  def _render(self):
    d = self.data
    ln = d.get('line') or {}
    today = d.get('today') or ''
    ship_by = str(d.get('ship_by') or '')[:10]
    late = bool(ship_by) and ship_by < today
    if hasattr(self, 'lbl_title'):
      self.lbl_title.text = (f"Multi-day work order {d['order']}-{ln.get('item')} · {ln.get('sku')}"
                             + (f" · {d['customer']}" if d.get('customer') else '')
                             + (f" · ship-by {self._mdy(ship_by)}" + (' (late)' if late else '') if ship_by else ''))
    pool = float(d.get('pool_inches') or 0)
    asm_day = float(d.get('assembler_day_in') or 0)
    crew = d.get('crew')
    partials = {'OK': 'ship each day', 'COMPLETE': 'hold until complete'}.get(d.get('partials'), 'not decided yet')
    more = d.get('days_more_est')
    if ln.get('done'):
      more_txt = 'built'
    elif more is not None:
      more_txt = f"about {more} more day(s) est."
    else:
      more_txt = 'not done by the end of the week shown (est.)'
    wos = ln.get('work_orders') or []
    if hasattr(self, 'lbl_sub'):
      self.lbl_sub.text = (f"{float(ln.get('qty') or 0):g} pc open · {float(ln.get('inches') or 0):,.0f} in"
                           f" · {float(ln.get('remaining') or 0):g} pc / {float(ln.get('remaining_inches') or 0):,.0f} in left ({ln.get('built_basis')})"
                           f" · day {ln.get('day_no')} · {more_txt}"
                           + (f" · WO {', '.join(wos)}" if wos else '')
                           + f" · pool {pool:,.0f} in/day · one assembler-day ≈ {asm_day:,.0f} in"
                           + (f" (crew {float(crew):g})" if crew else '') + f" · partials: {partials}")
    pz = d.get('paused')
    if hasattr(self, 'lbl_paused'):
      self.lbl_paused.visible = bool(pz)
      if pz:
        self.lbl_paused.text = (f"⏸ PAUSED to {pz.get('to')}" + (f": {pz.get('reason')}" if pz.get('reason') else '')
                                + ' - the order comes back first that morning.')
    # the order's other lines: links, never on this grid
    sibs = d.get('siblings') or []
    if hasattr(self, 'flow_siblings'):
      self.flow_siblings.visible = bool(sibs)
    if hasattr(self, 'lbl_sib_prompt'):
      self.lbl_sib_prompt.text = (f"{d['order']} also has {len(sibs)} other work order(s):" if sibs else '')
    for n in (1, 2, 3):
      lk = getattr(self, f'lk_sib{n}', None)
      if lk is None:
        continue
      if n <= len(sibs):
        sb = sibs[n - 1]
        lk.visible = True
        lk.tag = sb['item']
        lk.text = (f"-{sb['item']} {sb['sku']} · {float(sb['qty']):g} pc · {float(sb['inches']):,.0f} in"
                   + (' · built' if sb.get('done') else f" · {float(sb['remaining']):g} left")
                   + (f" · {float(sb['asm']):g} assembler(s)" if sb.get('asm') else ' · pool') + '  ')
      else:
        lk.visible = False
    if len(sibs) > 3 and hasattr(self, 'lbl_sib_prompt'):
      self.lbl_sib_prompt.text += f" (first 3 shown)"
    if hasattr(self, 'lbl_grid_note'):
      self.lbl_grid_note.text = ('assemblers: type a number per day (blank = carried from the day before, 0 = nobody). '
                                 'est. = what the 06:30 run would propose with those numbers; not a commitment. '
                                 '(rel nnn) = planned on an earlier release of the same line.  ')
    self._render_grid(d)
    # --- controls ---------------------------------------------------
    if hasattr(self, 'oc_controls'):
      self.oc_controls.visible = self.is_pc
    if hasattr(self, 'lbl_week'):
      self.lbl_week.text = d.get('week_label') or ''
    if hasattr(self, 'lk_clear_asm'):
      self.lk_clear_asm.visible = bool(d.get('has_headcount')) and self.is_pc
    self._cost_line(d)
    on_board = self._on_board_today()
    if hasattr(self, 'dd_partials'):
      self.dd_partials.selected_value = d.get('partials') or 'DECIDE'
      self.dd_partials.enabled = on_board
    if hasattr(self, 'btn_save_partials'):
      self.btn_save_partials.enabled = on_board
    if hasattr(self, 'lbl_partials_hint'):
      self.lbl_partials_hint.text = (f"the order's rule - applies to every line of {d['order']} on the board" if on_board
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
      self.lbl_pause_hint.text = ('paused - Resume brings the order back on the next 06:30 run' if pz
                                  else (f"the whole order {d['order']} leaves the plan until that date and comes back first" if on_board
                                        else 'pause applies to an order on the board'))
    if hasattr(self, 'cb_done'):
      self.cb_done.checked = bool(ln.get('done'))
      self.cb_done.enabled = ln.get('built_basis') != 'tablet Complete'
    if hasattr(self, 'lbl_done_hint'):
      self.lbl_done_hint.text = ('the tablet already says built' if ln.get('built_basis') == 'tablet Complete'
                                 else ('rides at 0 in on the board until the order ships; the retro records PC-reported' if ln.get('done')
                                       else 'use it when the floor finished and the tablet is silent'))

  # ------------------------------------------------------------------
  SLOTS = ('d1', 'd2', 'd3', 'd4', 'd5')

  def _render_grid(self, d):
    """The week grid in a DataGrid (dg_grid) + MultidayGridRow: columns are
    the five weekdays (titles set here; widths from the designer); the
    'assemblers' row carries a TextBox per day."""
    if not (hasattr(self, 'dg_grid') and hasattr(self, 'rp_grid')):
      return
    today = d.get('today') or ''
    days = (d.get('days') or [])[:5]
    self._slot_date = {}
    widths = {}
    for c in (self.dg_grid.columns or []):
      if c.get('width') not in (None, ''):
        widths[c.get('id')] = c.get('width')
    def col(cid, title):
      c = {'id': cid, 'title': title, 'data_key': cid}
      if cid in widths:
        c['width'] = widths[cid]
      return c
    cols = [col('line', '')]
    for i, x in enumerate(days):
      slot = self.SLOTS[i]
      self._slot_date[slot] = x['date']
      cols.append(col(slot, x['label']))
    self.dg_grid.columns = cols
    today_slot = next((sl for sl, dt in self._slot_date.items() if dt == today), None)
    asm_by_day = d.get('asm_by_day') or {}
    items = []
    for r in (d.get('grid_rows') or []):
      it = {'kind': r.get('kind'), 'label': r.get('label'), 'today_slot': today_slot}
      if r.get('kind') == 'asm':
        it['asm'] = {sl: (asm_by_day.get(dt) or {}) for sl, dt in self._slot_date.items()}
        it['on_asm'] = self._on_asm if self.is_pc else None
      else:
        for sl, dt in self._slot_date.items():
          it[sl] = (r.get('cells') or {}).get(dt)
      items.append(it)
    self.rp_grid.items = items

  def _on_asm(self, slot, text):
    """A weekday cell changed: save that day's number (blank = no entry,
    the previous day carries; 0 = nobody)."""
    dt = (getattr(self, '_slot_date', None) or {}).get(slot)
    if not dt:
      return
    txt = (text or '').strip()
    if txt:
      try:
        n = float(txt)
      except Exception:
        alert('Assemblers must be a number (blank = carried from the day before, 0 = nobody).')
        self.load()
        return
      if n < 0 or n > 50:
        alert('Assemblers: 0 to 50.')
        self.load()
        return
    ln = (self.data or {}).get('line') or {}
    r = anvil.server.call('ship_plan_multiday_plan_set', self.order, dt,
                          assemblers=(txt if txt else ''), rel=ln.get('rel'), item=self.item)
    if r.get('status') != 'success':
      alert(r.get('message', 'Save failed'))
    else:
      a = r.get('assemblers')
      Notification(f"{self.order}-{self.item} {self._mdy(dt)}: "
                   + (f"{float(a):g} assembler(s)" if a is not None else 'no entry - carries the day before'),
                   timeout=3).show()
    self.load()

  def _clear_asm(self, **event_args):
    if not confirm(f"Forget every headcount on {self.order}-{self.item}? The pool rule applies again from the next 06:30 run."):
      return
    ln = (self.data or {}).get('line') or {}
    r = anvil.server.call('ship_plan_multiday_plan_set', self.order, (self.data or {}).get('today'),
                          rel=ln.get('rel'), item=self.item, clear_assemblers=True)
    if r.get('status') != 'success':
      alert(r.get('message', 'Clear failed'))
    else:
      Notification(f"{self.order}-{self.item}: pool rule ({r.get('cleared')} day(s) cleared)", timeout=4).show()
    self.load()

  # ------------------------------------------------------------------
  def _cost_line(self, d):
    """The cost of the number on the first est. day (concept §4): n
    assemblers = n x one assembler-day = share of the pool; what is left
    of the crew and the day for everything else."""
    if not hasattr(self, 'lbl_cost'):
      return
    ln = d.get('line') or {}
    pool = float(d.get('pool_inches') or 0)
    asm_day = float(d.get('assembler_day_in') or 0)
    first_est = next((x for x in (d.get('days') or []) if x.get('kind') == 'est'), None)
    a = ((d.get('asm_by_day') or {}).get(first_est['date']) or {}).get('value') if first_est else None
    when = f" on {first_est['label'].replace(' est.', '')}" if first_est else ''
    if ln.get('done'):
      self.lbl_cost.text = 'Built - nothing left to plan.'
      return
    if not first_est:
      self.lbl_cost.text = ('No day of this week is left to plan - use ▶ for next week.' if (d.get('days') or []) else '')
      return
    per = (float(ln.get('inches') or 0) / float(ln.get('qty') or 1)) if float(ln.get('qty') or 0) > 0 else 0
    rem_in = float(ln.get('remaining_inches') or 0)
    if a is None:
      self.lbl_cost.text = (f"Pool rule{when}: the order takes up to {pool:,.0f} in a day, smallest line first"
                            + (f" (about {int(pool / per)} pieces of this line if it had the pool to itself)." if per else '.'))
      return
    if a == 0:
      self.lbl_cost.text = f"Nobody on it{when}: nothing of this work order is planned that day."
      return
    share = a * asm_day
    capped = share > pool + 1e-9
    share_eff = min(share, pool)
    pcs = int(share_eff / per) if per else 0
    days = int(-(-rem_in // share_eff)) if share_eff > 0 and rem_in > 0 else 0
    self.lbl_cost.text = (f"{a:g} assembler(s){when} = {share:,.0f} in a day"
                          + (f", capped at the pool ({pool:,.0f} in)" if capped else f" = {share_eff / pool * 100 if pool else 0:,.0f}% of the multi-day pool")
                          + f" ≈ {pcs} pieces of this line a day"
                          + (f" → about {days} day(s) for the {float(ln.get('remaining') or 0):g} left at that pace" if days else '')
                          + f" · {float(d.get('crew_left') or 0):g} crew left for {float(d.get('inches_left_for_others') or 0):,.0f} in of other work.")

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
    if not confirm(f"Pause the whole order {self.order} until {to}? Its lines leave today's plan; it comes back first that morning."):
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
    ln = d.get('line') or {}
    day = d.get('board_date') or d.get('today')
    if checked:
      if not confirm(f"Mark {self.order}-{self.item} built on your word? It rides at 0 in on the board and the retro records it as PC-reported."):
        self.cb_done.checked = False
        return
      r = anvil.server.call('ship_plan_multiday_plan_set', self.order, day, rel=ln.get('rel'), item=self.item, done=True)
      if r.get('status') != 'success':
        alert(r.get('message', 'Save failed'))
        self.cb_done.checked = False
        return
      Notification(f"{self.order}-{self.item}: built (PC's word)", timeout=4).show()
    else:
      r = anvil.server.call('ship_plan_multiday_plan_set', self.order, day, rel=ln.get('rel'), item=self.item, done=False)
      if r.get('status') != 'success':
        alert(r.get('message', 'Save failed'))
      else:
        Notification(f"{self.order}-{self.item}: not built after all", timeout=4).show()
    self.load()