from ._anvil_designer import ShipPlanCandidateRowTemplate
from anvil import *
import anvil.server
from datetime import date


QTY_TIP = ('e21 ordered quantity (orditem.qty - what is due by release). The number in parentheses is e21 balance due, shown only when the two disagree: a stale balance from an e21 editing path, not a shipment. Have order entry re-accept the line in e21 - the packing slip ships the balance.')


class ShipPlanCandidateRow(ShipPlanCandidateRowTemplate):
  """One (order, SKU) group in the candidates pane (releases already
  collapsed by vw_pc_ship_candidates).

  Designer components: lbl_order, lbl_what, lbl_sub, lbl_qty, lbl_shipby,
  lbl_load, lbl_flags, btn_add
  (+ btn_add_order '+SO' — hand-added in the designer, hasattr-guarded).

  Flags column (word + glyph, never color alone):
    ⛔ Xnn  = eligibility gate (hold / stock / no-WO) — row not plannable
    A/B    = urgency / value flag codes (flag system 040/041)
    ⚠ DEF  = classification defaulted conservative (PC review — 002_pratik)
    S      = non-standard elements present
    Z      = zero-touch (plans free against the cap)
    $      = surcharge applies (SpaceX / Termotran / duplex / mount)
  Tooltip lists priority score + every flag as 'code — note' (desktop).
  TAP-TO-SEE: lbl_flags is a LINK in the designer (same name); a tap
  opens a popup with the same breakdown — the Vibe boards have no
  hover. Guarded so the code runs clean while it is still a Label.
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item

    self.lbl_order.text = item['nx_order_numb']
    self.lbl_what.text = f"{item.get('sku_desc') or item['nx_sku']}"
    self.lbl_sub.text = f"{item.get('shipto_code') or ''} · {item.get('part_line') or item.get('bucket', '')}"
    # Qty = e21 ORDERED (orditem.qty); "(balance)" only when e21's balance
    # due disagrees (065 / C05).
    ordered = item.get('qty_ordered')
    balance = float(item['qty_open'])
    if ordered is None:
      self.lbl_qty.text = f"{balance:g}"
    elif abs(float(ordered) - balance) > 0.005:
      self.lbl_qty.text = f"{float(ordered):g} ({balance:g})"
      self.lbl_qty.foreground = '#C62828'
      self.lbl_qty.bold = True
      self.lbl_qty.tooltip = QTY_TIP
    else:
      self.lbl_qty.text = f"{float(ordered):g}"
    self.lbl_load.text = f"{item['load_inches']:,.1f} in"
    self.lbl_load.tooltip = (f"{item['load_minutes']:,.0f} min total, "
                             f"{item['surcharge_minutes']:,.0f} min surcharge, "
                             f"{item['n_source_rows']} source line(s)")
    # Tap-to-see for the boards (lbl_load = Link in the designer; a
    # Label raises ValueError here — swallowed so pre-swap runs clean).
    try:
      self.lbl_load.set_event_handler('click', self._show_load_details)
    except (ValueError, AttributeError):
      pass

    ship_by = item.get('ship_by_date')
    self.lbl_shipby.text = (ship_by or '')[:10]
    if ship_by and ship_by[:10] < date.today().isoformat():
      self.lbl_shipby.foreground = '#C62828'   # late — red, light-surface step
      self.lbl_shipby.bold = True

    glyphs = []
    if item.get('n_class_defaulted'):
      glyphs.append('⚠ DEF')
    if item.get('n_non_standard'):
      glyphs.append('S')
    if item.get('bucket') == 'ZERO':
      glyphs.append('Z')
    if float(item.get('surcharge_minutes') or 0) > 0.5:
      glyphs.append('$')
    # Flag system (040/041): gates first, then A/B codes, then glyphs.
    gate_codes = (item.get('gate_codes') or '').replace(',', ' ')
    flag_list = item.get('flags') or []
    if any(f.get('code') == 'C05' for f in flag_list):   # 065
      glyphs.append('⚠ QTY')
    ab_codes = ' '.join(f['code'] for f in flag_list
                        if f['code'][:1] in ('A', 'B'))
    parts = []
    if gate_codes:
      parts.append('⛔ ' + gate_codes)
    if ab_codes:
      parts.append(ab_codes)
    if glyphs:
      parts.append('  '.join(glyphs))
    self.lbl_flags.text = ' · '.join(parts)
    score = int(item.get('priority_score') or 0)
    tip_lines = [f'Priority score {score}']
    tip_lines += [f"{f['code']} — {f['note']}" for f in flag_list]
    if item.get('n_class_defaulted'):
      tip_lines.append('DEF — classification missing at plan time; '
                       'charged conservative (Non-Standard)')
    self.lbl_flags.tooltip = '\n'.join(tip_lines)
    if item.get('is_gated'):
      # Gated — red (#C62828, light-surface step) + the ⛔ word/glyph,
      # so color is never the only signal.
      self.lbl_flags.foreground = '#C62828'
      self.lbl_flags.bold = True
    elif item.get('n_class_defaulted'):
      self.lbl_flags.foreground = '#a16207'
    self._flag_tip_lines = tip_lines
    # Tap-to-see (touch boards): Link fires 'click'; a Label raises
    # ValueError on this call — swallowed so nothing breaks pre-swap.
    try:
      self.lbl_flags.set_event_handler('click', self._show_flag_details)
    except (ValueError, AttributeError):
      pass

    # Gated rows are not plannable — the line-level add is hidden.
    # (+SO stays visible: adding the ORDER is a PC judgment call on a
    # mixed order — v1 gates are advisory there; a server-side guard
    # in add_order is a candidate once the bake settles behavior.)
    on_add = item.get('on_add')
    self.btn_add.visible = (on_add is not None
                            and not item.get('is_gated'))
    if on_add:
      self.btn_add.set_event_handler('click',
                                     lambda **e: on_add(self.item))

    # btn_add_order ('+SO') is hand-added in the designer — guarded so
    # this code runs clean before Dave adds it.
    on_add_order = item.get('on_add_order')
    if hasattr(self, 'btn_add_order'):
      self.btn_add_order.visible = on_add_order is not None
      if on_add_order:
        self.btn_add_order.set_event_handler(
          'click', lambda **e: on_add_order(self.item))

  # ---------------------------------------------------------------
  # Tap-to-see popups (the 75" Vibe boards have no hover). Data is
  # already in the row item — no round trip. Tight line spacing: every
  # Label gets spacing_above/below 'none'; one line per fact.
  # ---------------------------------------------------------------
  @staticmethod
  def _line(text, size=15, bold=False, color=None):
    lbl = Label(text=text, font_size=size, bold=bold,
                spacing_above='none', spacing_below='none')
    if color:
      lbl.foreground = color
    return lbl

  def _popup(self, title, lines):
    panel = ColumnPanel(spacing_above='none', spacing_below='none')
    for l in lines:
      panel.add_component(l)
    alert(content=panel, title=title, buttons=[('Close', None)],
          large=False)

  def _headline(self):
    item = self.item
    return self._line(f"{item['nx_order_numb']} · "
                      f"{item.get('sku_desc') or item['nx_sku']}",
                      size=17, bold=True)

  def _show_flag_details(self, **event_args):
    item = self.item
    gated = bool(item.get('is_gated'))
    lines = [self._headline()]
    score_txt = self._flag_tip_lines[0]
    if gated:
      lines.append(self._line(score_txt + '  —  GATED: not plannable as '
                              'things stand', bold=True, color='#C62828'))
    else:
      lines.append(self._line(score_txt))
    for f in (item.get('flags') or []):
      is_gate = f['code'].startswith('X')
      lines.append(self._line(f"{f['code']}  {f['name']} — {f['note'] or ''}",
                              bold=is_gate,
                              color='#C62828' if is_gate else None))
    if item.get('n_class_defaulted'):
      lines.append(self._line('DEF  classification missing at plan time — '
                              'charged conservative (Non-Standard)',
                              color='#a16207'))
    if len(lines) == 2 and not item.get('flags'):
      lines.append(self._line('No flags on this line.'))
    self._popup('Flags', lines)

  def _show_load_details(self, **event_args):
    """Load (work-inches) breakdown — mirrors the desktop tooltip."""
    item = self.item
    mins = float(item.get('load_minutes') or 0)
    sur = float(item.get('surcharge_minutes') or 0)
    lines = [self._headline(),
             self._line(f"{float(item.get('load_inches') or 0):,.1f} work-inches",
                        size=16, bold=True),
             self._line(f"{mins:,.0f} min total  ·  {sur:,.0f} min surcharge"),
             self._line(f"{item.get('n_source_rows') or 0} source line(s)  ·  "
                        f"qty open {float(item.get('qty_open') or 0):g}  ·  "
                        f"bucket {item.get('bucket') or '—'}")]
    if item.get('n_non_standard'):
      lines.append(self._line(f"{item['n_non_standard']} non-standard line(s)"))
    if item.get('n_class_defaulted'):
      lines.append(self._line('DEF  classification missing — charged '
                              'conservative (Non-Standard)', color='#a16207'))
    self._popup('Load', lines)