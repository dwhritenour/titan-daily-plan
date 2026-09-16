from ._anvil_designer import ship_planTemplate
from anvil import *
import anvil.server
from datetime import date, datetime, timedelta

from .ShipPlanParamsPanel import ShipPlanParamsPanel


def line_label(item, with_sku=True):
  """066: the e21 line - '595613-20' (release shown only when not 001),
  + ' / SKU'. Works for plan lines, candidates and variance rows."""
  o = item.get('nx_order_numb') or item.get('order') or ''
  r = (item.get('nx_rel_numb') or item.get('rel') or '').strip()
  i = (item.get('nx_item_no') or item.get('item') or '').strip()
  s = f"{o}-{r}-{i}" if (r or i) else f"{o}"     # always order-release-item
  sku = item.get('nx_sku') or item.get('sku')
  return f"{s} / {sku}" if (with_sku and sku) else s


class ship_plan(ship_planTemplate):
  """
    Daily Ship Plan — Week-2 release (PC builds/publishes; Sales read-only).
    Spec: documents/Ship_Plan_Form_Specification.docx v1.0.0.

    Designer components this code binds by name (§2 of the spec):
      oc_header:    dp_plan_date, lbl_plan_status, btn_publish,
                    lk_history, lk_params
      oc_capacity:  lbl_capacity_line, cv_capacity (Canvas, ~28px tall,
                    full width — the bar is DRAWN, so it needs no CSS),
                    lbl_crew, lbl_snapshot_warn, lk_overrides
      flow_filters: tb_search, dd_bucket, cb_due_window, cb_special_only,
                    cb_defaulted_only, lbl_feed_note,
                    dd_flag (Flag filter) + btn_flag_legend ('Flag legend')
                    — both hand-added 2026-09-01, hasattr-guarded
                    2026-09-15: search / bucket / flag / non-standard /
                    unclassified filter BOTH grids. The plan grid filters
                    its cached lines client-side (no round trip); the due
                    window is a candidate-pool horizon and leaves the plan
                    alone (a pulled-ahead line must stay visible).
      grids:        rp_candidates (ShipPlanCandidateRow),
                    rp_plan_lines (ShipPlanLineRow), cb_show_removed
      row buttons (hand-added in the designer, hasattr-guarded until
                    then): candidate row btn_add_order ('+SO'),
                    line row btn_priority ('★')
      timer:        timer_drift (interval 300)

    The client computes NOTHING — capacity, loads, classification and the
    already-planned guard are all server/view-side. Drift is only ever
    DISPLAYED here; the Refresh action is the one re-total path.
  """

  def __init__(self, **properties):
    self.init_components(**properties)

    # Deep links from the morning texts (2026-09-07): #retro = PC/Sales
    # answer their retro questions, #retrev = Dave confirms readings.
    # Same pattern as the Hub app's #pick / #dock routes.
    _hash = get_url_hash()
    if _hash == 'retro':
      open_form('retro')
      return
    if _hash == 'retrev':
      open_form('retro_review')
      return
    if _hash == 'done':                     # wo-1.0.0: completed WOs by day
      open_form('wo_completed')
      return
    if _hash == 'shipped':                  # ship-1.0.0: e21 shipments by day
      open_form('orders_shipped')
      return

    # --- role gate (Wk-2: Sales = read-only) -------------------------
    # TODO(Dave/Chuck): wire to the app's real role source, same pattern
    # as the other PC forms. is_pc=False hides every mutating control.
    self.is_pc = True

    for c in (self.btn_publish, self.lk_params, self.lk_overrides):
      c.visible = self.is_pc

    # --- event wiring -------------------------------------------------
    self.dp_plan_date.set_event_handler('change', self._date_changed)
    self.btn_publish.set_event_handler('click', self._publish_click)
    self.lk_history.set_event_handler('click', self._history_click)
    self.lk_params.set_event_handler('click', self._params_click)
    self.lk_overrides.set_event_handler('click', self._overrides_click)
    self.tb_search.set_event_handler('pressed_enter', self._filters_changed)
    self.dd_bucket.set_event_handler('change', self._filters_changed)
    for cb in (self.cb_due_window, self.cb_special_only,
               self.cb_defaulted_only):
      cb.set_event_handler('change', self._filters_changed)
    self.cb_show_removed.set_event_handler('change', self._plan_filter_changed)
    self.timer_drift.set_event_handler('tick', self._timer_tick)
    # Canvas fires 'reset' when it (re)sizes — redraw the bar then.
    self.cv_capacity.set_event_handler('reset', self._draw_capacity_bar)

    self.dd_bucket.items = [('All product', None), ('Strainers', 'ST'),
                            ('Zero-touch', 'ZERO'), ('Other', 'OTHER')]
    self.cb_due_window.checked = True

    # --- Flag filter + legend (2026-09-01; designer controls guarded) --
    self._flag_defs = []
    if hasattr(self, 'dd_flag'):
      self.dd_flag.items = self._flag_items()
      self.dd_flag.selected_value = 'ALL'
      self.dd_flag.set_event_handler('change', self._filters_changed)
    if hasattr(self, 'btn_flag_legend'):
      self.btn_flag_legend.set_event_handler('click', self._show_flag_legend)
    # --- Clear filters (2026-09-15): designer button if present, else a
    #     code-added Link at the end of the filter row -----------------
    if hasattr(self, 'btn_clear_filters'):
      self.btn_clear_filters.set_event_handler('click', self._clear_filters)
    else:
      self.flow_filters.add_component(
        Link(text='Clear filters', icon='fa:times-circle', font_size=13,
             tooltip='Reset every filter on both grids'))
      self.flow_filters.get_components()[-1].set_event_handler(
        'click', self._clear_filters)
    # --- AI agent (proposal engine, 051/052; designer links guarded) ---
    self._proposal = None
    if hasattr(self, 'lk_proposal'):
      self.lk_proposal.set_event_handler('click', self._show_proposal)
    if hasattr(self, 'lk_accept_all'):
      self.lk_accept_all.set_event_handler('click', self._accept_all_proposed)
      self.lk_accept_all.visible = False
    if hasattr(self, 'lk_run_agent'):
      self.lk_run_agent.text = 'Run agent'      # code owns the label
      self.lk_run_agent.set_event_handler('click', self._run_agent_now)
      self.lk_run_agent.visible = self.is_pc
    if hasattr(self, 'lk_retro'):
      self.lk_retro.text = 'Retro'              # code owns the label
      self.lk_retro.set_event_handler('click', lambda **e: open_form('retro'))
      self.lk_retro.visible = True              # everyone reads the retro
    if hasattr(self, 'lk_retro_review'):
      self.lk_retro_review.text = 'Retro review'
      self.lk_retro_review.set_event_handler('click', lambda **e: open_form('retro_review'))
    if hasattr(self, 'lk_completed'):        # wo-1.0.0 (2026-09-16)
      self.lk_completed.text = 'WO Progress'
      self.lk_completed.set_event_handler('click', lambda **e: open_form('wo_completed'))
    if hasattr(self, 'lk_shipped'):          # ship-1.0.0 (2026-09-16)
      self.lk_shipped.text = 'WO Shipped'
      self.lk_shipped.set_event_handler('click', lambda **e: open_form('orders_shipped'))

    # Horizon lives SERVER-side now: 'auto' tells get_candidates to
    # read PLAN_HORIZON_DAYS itself, so a saved change applies on the
    # very next refresh (the old client-cached 10 never updated).
    self._capacity = None
    self._lines = []

    # --- click-bounce shield (2026-08-26 tape) ---------------------
    # Recorded pointer hardware emitting DUPLICATE press events
    # 35-50ms apart at normal clicking speed (a hand can't click that
    # fast). Harmless on buttons; fatal on native dropdowns, which
    # TOGGLE per press — the echo shuts the picker 40ms after the real
    # click opens it ("takes three clicks"). Swallow any mouse press
    # arriving <80ms after the previous one, document-wide. Touch taps
    # pass through untouched. Pure JS, no CSS — theme standard intact.
    try:
      from anvil.js import window as _w
      _w.eval("""
        (function(){
          if (window.__bounceShield) return; window.__bounceShield = 1;
          var last = {};
          ['pointerdown','mousedown','mouseup','click'].forEach(function(t){
            document.addEventListener(t, function(e){
              if (e.pointerType === 'touch') return;
              var now = performance.now();
              if (last[t] !== undefined && now - last[t] < 80) {
                e.stopImmediatePropagation(); e.preventDefault(); return;
              }
              last[t] = now;
            }, true);
          });
        })();
      """)
    except Exception:
      pass          # shield is best-effort; the app runs fine without it

    self.dp_plan_date.date = self._next_business_day()
    self.load_day()

  # =================================================================
  # Loading
  # =================================================================
  @staticmethod
  def _next_business_day():
    d = date.today()
    while d.weekday() >= 5:
      d += timedelta(days=1)
    return d

  @property
  def plan_date(self):
    return self.dp_plan_date.date or self._next_business_day()

  def load_day(self):
    """ONE server round trip for the whole day (plan + candidates).
    Every Anvil call pays ~1s of transport before its query runs, so the
    old two sequential calls were most of the after-dialog lag."""
    result = anvil.server.call(
      'ship_plan_get_day', self.plan_date,
      search=self.tb_search.text or None,
      bucket=self.dd_bucket.selected_value,
      due_within_days='auto' if self.cb_due_window.checked else None,
      special_only=self.cb_special_only.checked,
      defaulted_only=self.cb_defaulted_only.checked,
      flag_filter=self._flag_filter())
    if result.get('status') != 'success':
      alert(f"Error loading plan: {result.get('message', 'Unknown error')}")
      return
    defs = result.get('flag_definitions')
    if defs is not None and defs != self._flag_defs:
      self._flag_defs = defs
      if hasattr(self, 'dd_flag'):
        keep = self.dd_flag.selected_value
        self.dd_flag.items = self._flag_items()
        self.dd_flag.selected_value = keep or 'ALL'
    plan = result['plan']
    self._capacity = plan['capacity']
    self._render_header(plan.get('header'))
    self._render_capacity(plan['planned_inches'],
                          plan.get('stretch_inches', 0))
    self._render_lines(plan['lines'])
    self._render_proposal(plan)
    self._render_candidates(result['candidates'])

  def refresh_candidates(self, **event_args):
    result = anvil.server.call(
      'ship_plan_get_candidates',
      search=self.tb_search.text or None,
      bucket=self.dd_bucket.selected_value,
      due_within_days='auto' if self.cb_due_window.checked else None,
      special_only=self.cb_special_only.checked,
      defaulted_only=self.cb_defaulted_only.checked,
      flag_filter=self._flag_filter())
    if result.get('status') != 'success':
      alert(f"Error loading candidates: {result.get('message')}")
      return
    self._render_candidates(result)

  def _render_candidates(self, result):
    items = result['candidates']
    for it in items:
      it['on_add'] = self._add_line if self.is_pc else None
      it['on_add_order'] = self._add_order if self.is_pc else None
      it['on_release_hold'] = self._release_hold if self.is_pc else None   # 068
    self.rp_candidates.items = items
    # Header says what the list IS — gated-last / priority-first
    # (flag system 041), and exactly how much of the match survived
    # the cap. Silent truncation caused the
    # "horizon does nothing / list ends at 8/24" report of 2026-08-27.
    if hasattr(self, 'lbl_candidates_header'):
      total = result.get('total_count')
      hz = result.get('horizon_days')
      txt = 'UP NEXT — CANDIDATES'
      if hz:
        txt += f' (due within {hz} business days)'
      if total is not None:
        if total > len(items):
          txt += f' — showing top {len(items)} of {total} by priority'
        else:
          txt += f' — {total}'
      n_gated = sum(1 for it in items if it.get('is_gated'))
      if n_gated:
        txt += f' · {n_gated} gated'
      self.lbl_candidates_header.text = txt
    self._render_feed_chip(result)

  def _render_feed_chip(self, result):
    """ALWAYS-ON orders-feed freshness chip (server-computed age, so no
    browser-clock skew). Fresh <= 120 min; stale beyond that."""
    age = result.get('feed_age_minutes')
    ts = (result.get('feed_last_updated') or '')[11:16]
    self.lbl_feed_note.visible = True
    if age is None:
      self.lbl_feed_note.text = 'Orders ⚠ feed empty'
      self.lbl_feed_note.foreground = '#a16207'
    elif age > 120:
      hrs = age / 60.0
      self.lbl_feed_note.text = f'Orders ⚠ {hrs:.1f} h old ({ts})'
      self.lbl_feed_note.foreground = '#a16207'
    else:
      self.lbl_feed_note.text = f'Orders ✓ {ts}'
      self.lbl_feed_note.foreground = '#006300'

  def _filters_changed(self, **event_args):
    """2026-09-15: one filter row, two grids. The plan re-renders from
    the cached lines first (instant), then the candidates round-trip."""
    self._render_lines(self._lines)
    self.refresh_candidates()

  def _plan_filter_changed(self, **event_args):
    self._render_lines(self._lines)

  def _clear_filters(self, **event_args):
    """Back to the opening state of the filter row (due window ON, as at
    load); both grids re-render. Show removed is a plan-grid switch, not
    a filter, so it is left alone."""
    self.tb_search.text = ''
    self.dd_bucket.selected_value = None
    if hasattr(self, 'dd_flag'):
      self.dd_flag.selected_value = 'ALL'
    self.cb_due_window.checked = True
    self.cb_special_only.checked = False
    self.cb_defaulted_only.checked = False
    self._filters_changed()

  def _line_matches(self, l):
    """Same meaning as the server-side candidate WHERE (get_candidates):
    search = order / SKU / description / ship-to substring (plus the
    order-release-item label, so '607462-002' finds a release); bucket;
    Non-Standard; Unclassified; flag dropdown on is_gated / flag_codes.
    Fields ride on each line from get_plan (2026-09-15 enrichment); a line
    e21 no longer knows (shipped) has no bucket or flags and only matches
    the search."""
    q = (self.tb_search.text or '').strip().upper()
    if q:
      hay = ' '.join(str(l.get(k) or '') for k in
                     ('nx_order_numb', 'nx_sku', 'sku_desc', 'shipto_code',
                      'customer', 'line_label')).upper()
      if q not in hay:
        return False
    bucket = self.dd_bucket.selected_value
    if bucket in ('ST', 'ZERO', 'OTHER') and l.get('bucket') != bucket:
      return False
    if self.cb_special_only.checked and not int(l.get('n_non_standard') or 0):
      return False
    if self.cb_defaulted_only.checked and not int(l.get('n_class_defaulted') or 0):
      return False
    ff = (self._flag_filter() or 'ALL').upper()
    if ff != 'ALL':
      gated = bool(l.get('is_gated'))
      codes = [c for c in (l.get('flag_codes') or '').split(',') if c]
      if ff == 'PLANNABLE' and gated:
        return False
      if ff == 'GATED' and not gated:
        return False
      if ff in ('ANY_A', 'ANY_B') and not any(c.startswith(ff[-1]) for c in codes):
        return False
      if len(ff) == 3 and ff[0] in 'XABC' and ff[1:].isdigit() and ff not in codes:
        return False
    return True

  def _plan_filter_active(self):
    return bool((self.tb_search.text or '').strip()
                or self.dd_bucket.selected_value in ('ST', 'ZERO', 'OTHER')
                or self.cb_special_only.checked
                or self.cb_defaulted_only.checked
                or (self._flag_filter() or 'ALL').upper() != 'ALL')

  # =================================================================
  # Flag filter + legend (2026-09-01) — both read the LIVE definitions
  # that ride along with get_day; nothing about flags is hardcoded here.
  # =================================================================
  _FLAG_CATS = (('X', 'X — Eligibility gates', '#C62828'),
                ('A', 'A — Urgency & commitments', None),
                ('B', 'B — Business value', None),
                ('C', 'C — Informational context', None))

  def _flag_filter(self):
    if not hasattr(self, 'dd_flag'):
      return None
    v = self.dd_flag.selected_value
    return None if v in (None, 'ALL') else v

  def _flag_items(self):
    items = [('All flags', 'ALL'),
             ('Plannable only (no gate)', 'PLANNABLE'),
             ('Gated only', 'GATED'),
             ('Any A — urgency', 'ANY_A'),
             ('Any B — value', 'ANY_B')]
    for f in self._flag_defs:
      if f.get('active') and f.get('category') in ('X', 'A', 'B'):
        items.append((f"{f['flag_code']}  {f['flag_name']}", f['flag_code']))
    return items

  @staticmethod
  def _legend_line(text, size=15, bold=False, color=None):
    lbl = Label(text=text, font_size=size, bold=bold,
                spacing_above='none', spacing_below='none')
    if color:
      lbl.foreground = color
    return lbl

  def _show_flag_legend(self, **event_args):
    """Legend popup — same tight, large-type style as the row popups
    (the boards have no hover). Inactive rows greyed, never hidden."""
    defs = self._flag_defs or []
    panel = ColumnPanel(spacing_above='none', spacing_below='none')
    panel.add_component(self._legend_line(
      'Flags — what they mean and how they rank', size=17, bold=True))
    panel.add_component(self._legend_line(
      'Gates (X) are filtered out of ranking and shown with their reason. '
      'Ungated rows rank by the sum of their flag weights, then oldest '
      'ship-by. Tap a row\'s flags for its own breakdown.', size=13,
      color='#595959'))
    for cat, title, color in self._FLAG_CATS:
      rows = [f for f in defs if f.get('category') == cat]
      if not rows:
        continue
      panel.add_component(self._legend_line(' ', size=6))
      panel.add_component(self._legend_line(title, size=16, bold=True,
                                            color=color))
      for f in rows:
        active = bool(f.get('active'))
        txt = f"{f['flag_code']}  {f['flag_name']} — {f.get('description') or ''}"
        if cat == 'X':
          txt += '   · gate'
        elif cat == 'C':
          glyph = f.get('display_glyph')
          txt += f"   · shown as {glyph}" if glyph else ''
        else:
          txt += f"   · weight {int(f.get('weight') or 0)}"
          thr = f.get('threshold_value')
          if thr:
            txt += f", threshold ${float(thr):,.0f}"
        if not active:
          txt += '   (not yet active)'
        panel.add_component(self._legend_line(
          txt, bold=(cat == 'X' and active),
          color='#8a8a8a' if not active
          else ('#C62828' if cat == 'X' else None)))
    alert(content=panel, title='Flag legend',
          buttons=[('Close', None)], large=True)

  def _date_changed(self, **event_args):
    self.load_day()

  # =================================================================
  # Rendering
  # =================================================================
  def _render_header(self, header):
    # Colors are the DAYLIGHT status steps (light Board Mode palette,
    # green-lit 2026-08-24): green/amber as TEXT use the dark steps
    # (#006300 / #a16207) so they hold contrast on the light surface;
    # the bright fills (#0ca30c / #fab219) are reserved for the meter.
    if header is None:
      self.lbl_plan_status.text = 'NO PLAN YET'
      self.lbl_plan_status.foreground = '#898781'
      self.btn_publish.enabled = False
      return
    status = header['plan_status']
    txt = status.upper()
    if status == 'Published':
      # "PUBLISHED 8:52 · rev 3 (10:14)" — post-publish edits are fine
      # (real life happens) but they are VISIBLE, never silent. The
      # counter lives server-side (_bump_revision); Draft edits are free.
      pub_t = str(header.get('published_at') or '')[11:16]
      if pub_t:
        txt = f'PUBLISHED {pub_t}'
      rev = header.get('revision_no') or 0
      if rev:
        rev_t = str(header.get('last_revised_at') or '')[11:16]
        txt += f' · rev {rev}' + (f' ({rev_t})' if rev_t else '')
      self.lbl_plan_status.foreground = '#006300'
      self.lbl_plan_status.tooltip = (
        f"published by {header.get('published_by')} "
        f"at {header.get('published_at')}")
    else:
      self.lbl_plan_status.foreground = '#a16207'
    self.lbl_plan_status.text = txt
    self.btn_publish.enabled = self.is_pc and status == 'Draft'

  def _render_capacity(self, planned, stretch=0):
    cap = self._capacity or {}
    eff_cap = cap.get('capacity_inches_effective') or 0
    self._planned = planned
    self._stretch = stretch or 0
    txt = f"Planned {planned:,.0f} of {eff_cap:,.0f} work-inches"
    if self._stretch:
      # Stretch is "if capacity permits" — it never counts against the
      # cap, so it rides OUTSIDE the Planned number and the meter fill.
      txt += f"  · +{self._stretch:,.0f} if time"
    self.lbl_capacity_line.text = txt
    self._draw_capacity_bar()
    self.lbl_crew.text = cap.get('capacity_detail', '')
    # ALWAYS-ON attendance freshness chip (Dave 2026-08-25): positive
    # confirmation beats an appearing warning — a missing chip would be
    # indistinguishable from "all good". Word + color, never color alone.
    self.lbl_snapshot_warn.visible = True
    if cap.get('is_estimate'):
      self.lbl_snapshot_warn.text = 'Attendance — estimate (no snapshot yet)'
      self.lbl_snapshot_warn.foreground = '#898781'   # muted
    elif cap.get('snapshot_stale'):
      self.lbl_snapshot_warn.text = (
        f"Attendance ⚠ {cap.get('snapshot_date')} — morning file not loaded")
      self.lbl_snapshot_warn.foreground = '#a16207'   # amber text step
    else:
      self.lbl_snapshot_warn.text = (
        f"Attendance ✓ {cap.get('snapshot_date') or 'today'}")
      self.lbl_snapshot_warn.foreground = '#006300'   # green text step

  def _draw_capacity_bar(self, **event_args):
    """Capacity meter, fully DRAWN (no CSS — standard-compliant).
    Normal day: blue fill, faint amber watch zone from 85% to the cap,
    planned value printed at the data end, headroom above, named ticks
    below. Over capacity: amber to the cap, then a red overflow segment
    with '+N over'. Future-day estimate: light-blue 45-degree hatched
    fill labeled 'estimate'. Set cv_capacity HEIGHT to 64 in the
    designer for the full layout — below 60px it draws the bar only."""
    import math
    cap = self._capacity or {}
    eff_cap = cap.get('capacity_inches_effective') or 0
    planned = getattr(self, '_planned', 0)
    ratio = (planned / eff_cap) if eff_cap else 0
    over = ratio > 1.0 and eff_cap > 0
    est = bool(cap.get('is_estimate'))

    c = self.cv_capacity
    w, h = c.get_width(), c.get_height()
    if not w or not h:
      return
    c.clear_rect(0, 0, w, h)

    full = h >= 60                       # room for captions + headroom text
    top = 18 if full else 0
    bot = 16 if full else 0
    # The BAR keeps meter proportions no matter the canvas height — extra
    # height becomes breathing room around a ~32px meter, never a thicker
    # slab (2026-08-25: at height 110 the old math drew a 76px brick).
    bh = min(h - top - bot, 32)
    y_off = max(0, (h - (top + bh + bot)) // 2)
    y0 = y_off + top
    y1 = y0 + bh
    rad = max(4, min(10, bh // 3))
    mid = (y0 + y1) / 2

    def rrect(x0, x1, round_right=True, round_left=True):
      """Filled rect from x0..x1 with optionally rounded corners."""
      if x1 - x0 < 1:
        return
      rr = min(rad, (x1 - x0) / 2)
      rl = rr if round_left else 0
      rt = rr if round_right else 0
      c.begin_path()
      c.move_to(x0 + rl, y0)
      c.line_to(x1 - rt, y0)
      if rt:
        c.arc(x1 - rt, y0 + rt, rt, -math.pi / 2, 0)
      c.line_to(x1, y1 - rt)
      if rt:
        c.arc(x1 - rt, y1 - rt, rt, 0, math.pi / 2)
      c.line_to(x0 + rl, y1)
      if rl:
        c.arc(x0 + rl, y1 - rl, rl, math.pi / 2, math.pi)
      c.line_to(x0, y0 + rl)
      if rl:
        c.arc(x0 + rl, y0 + rl, rl, math.pi, 3 * math.pi / 2)
      c.close_path()
      c.fill()

    # Cap position: full width normally; reserve an overflow lane when over.
    cap_x = w * 0.86 if over else w - 2
    zx = cap_x * 0.85                    # 85% watch threshold

    # Track + watch-zone tint (85% -> cap)
    c.fill_style = '#efeeea'
    rrect(0, cap_x)
    c.fill_style = '#f7ecd4'
    c.fill_rect(zx, y0, max(0, cap_x - zx - rad), bh)
    rrect(max(zx, cap_x - 2 * rad), cap_x, round_left=False)

    ov_w = 0
    if est:
      # Estimate: light-blue hatched fill — 'not real yet'
      fx = min(ratio, 1.0) * cap_x
      c.fill_style = '#9ec5f4'
      rrect(0, fx, round_right=False)
      c.stroke_style = '#2a78d6'
      c.line_width = 2
      hx = 4
      while hx + bh < fx:
        c.begin_path()
        c.move_to(hx, y1 - 1)
        c.line_to(hx + bh, y0 + 1)
        c.stroke()
        hx += 12
    elif over:
      # Amber to the cap, 2px gap, red overflow segment sized by overage
      c.fill_style = '#fab219'
      rrect(0, cap_x, round_right=False)
      ov_w = min((ratio - 1.0) * cap_x, w - cap_x - 6)
      if ov_w > 3:
        c.fill_style = '#fcfcfb'
        c.fill_rect(cap_x, y0, 2, bh)
        c.fill_style = '#d03b3b'
        rrect(cap_x + 2, cap_x + 2 + ov_w, round_left=False)
    else:
      fx = ratio * cap_x
      c.fill_style = '#fab219' if ratio >= 0.85 else '#2a78d6'
      rrect(0, fx, round_right=False)
      # Stretch: LIGHTER segment appended after the fill — visible work,
      # but visually "not load" (it doesn't count against the cap).
      stretch = getattr(self, '_stretch', 0)
      if stretch and eff_cap:
        sx = min((planned + stretch) / eff_cap, 1.0) * cap_x
        if sx > fx + 4:
          c.fill_style = '#cfe1f8'
          c.fill_rect(fx + 2, y0 + 2, sx - fx - 4, bh - 4)
      c.fill_style = '#fcfcfb'             # 2px surface gap at the data end
      c.fill_rect(fx, y0, 2, bh)

    if not full:
      return

    # ---- text layer (word + position carry meaning, never color alone) ----
    c.text_baseline = 'middle'
    c.font = 'bold 13px sans-serif'
    if over:
      c.fill_style = '#5b4300'             # cap value inside the amber
      c.text_align = 'right'
      c.fill_text(f"{eff_cap:,.0f}", cap_x - 8, mid)
      overage = f"+{planned - eff_cap:,.0f} over"
      if ov_w > 78:                        # room inside the red segment
        c.fill_style = '#ffffff'
        c.text_align = 'right'
        c.fill_text(overage, cap_x + ov_w - 6, mid)
      else:
        c.fill_style = '#d03b3b'
        c.text_align = 'left'
        c.fill_text(overage, cap_x + 2 + ov_w + 6, mid)
    else:
      fx = min(ratio, 1.0) * cap_x
      txt = f"{planned:,.0f}"
      if fx > 70:
        c.fill_style = '#0d366b' if est else '#ffffff'
        c.text_align = 'right'
        c.fill_text(txt, fx - 8, mid)
      else:
        c.fill_style = '#52514e'
        c.text_align = 'left'
        c.fill_text(txt, fx + 8, mid)

    # threshold ticks, extended past the bar, with small captions below
    c.fill_style = '#fab219'
    c.fill_rect(zx - 1, y0 - 5, 2, bh + 10)
    c.fill_style = '#d03b3b'
    c.fill_rect(cap_x - 1, y0 - 5, 2, bh + 10)
    c.font = '10px sans-serif'
    c.fill_style = '#898781'
    c.text_baseline = 'top'
    c.text_align = 'center'
    c.fill_text('85%', zx, y1 + 5)
    if over:
      c.fill_text(f"cap {eff_cap:,.0f}", cap_x, y1 + 5)
    else:
      c.text_align = 'right'
      c.fill_text(f"cap {eff_cap:,.0f}", w - 2, y1 + 5)

    # headroom (or 'estimate' word) above the right/left end of the bar
    c.font = 'bold 12px sans-serif'
    c.text_baseline = 'bottom'
    if est:
      c.text_align = 'left'
      c.fill_style = '#898781'
      c.fill_text('estimate', 0, y0 - 4)
    if not over and eff_cap:
      c.text_align = 'right'
      c.fill_style = '#898781' if est else '#006300'
      c.fill_text(f"{eff_cap - planned:,.0f} in headroom", w - 2, y0 - 4)

  def _render_lines(self, lines):
    self._lines = lines
    visible = [l for l in lines
               if self.cb_show_removed.checked or l['line_status'] != 'Removed']
    shown = [l for l in visible if self._line_matches(l)]
    # Header says what the grid IS - a filtered plan must never look like
    # a short plan (the capacity bar still totals EVERY active line).
    if hasattr(self, 'lbl_plan_header'):
      txt = "THIS DAY'S PLAN"
      if self._plan_filter_active():
        txt += f' — showing {len(shown)} of {len(visible)} (filtered)'
      elif visible:
        txt += f' — {len(visible)}'
      self.lbl_plan_header.text = txt
    for it in shown:
      if self.is_pc:
        it['on_recalc'] = self._recalc_line
        it['on_snooze'] = self._snooze_line
        it['on_remove'] = self._remove_line
        it['on_priority'] = self._priority_dialog
        it['on_confirm'] = self._confirm_line
      else:
        it['on_recalc'] = it['on_snooze'] = it['on_remove'] = None
        it['on_priority'] = it['on_confirm'] = None
    self.rp_plan_lines.items = shown

  # =================================================================
  # Actions (all PC-only; item-callback pattern like routing_status)
  # =================================================================
  def _add_line(self, item):
    result = anvil.server.call('ship_plan_add_line', self.plan_date,
                               item['nx_order_numb'], item['nx_rel_numb'],
                               item['nx_item_no'])
    if result.get('status') != 'success':
      alert(result.get('message', 'Add failed'))
      return
    if result.get('over_capacity'):
      self._cap_hit_dialog(item['nx_order_numb'],
                           [(item['nx_rel_numb'], item['nx_item_no'])], result)
    self.load_day()

  def _release_hold(self, item):
    """068: PC releases a material hold (X05) - stock arrived, or the
    retro answer was wrong. Reason is optional; the line re-ranks now."""
    lbl = line_label(item, with_sku=False)
    tb = TextBox(placeholder='why (optional) - e.g. stock received')
    ok = alert(content=tb, title=f'Release material hold on {lbl}?',
               buttons=[('Release', True), ('Cancel', False)], large=False)
    if not ok:
      return
    r = anvil.server.call('ship_plan_release_hold', item['nx_order_numb'],
                          item['nx_rel_numb'], item['nx_item_no'],
                          tb.text or None)
    if r.get('status') != 'success':
      alert(r.get('message', 'Release failed'))
      return
    Notification(f'Hold released on {lbl}', timeout=3).show()
    self.load_day()

  def _add_order(self, item):
    """Whole sales order onto the day — every open SKU, ONE server
    transaction, one cap dialog for the lot (not one per line)."""
    order = item['nx_order_numb']
    result = anvil.server.call('ship_plan_add_order', self.plan_date, order)
    if result.get('status') != 'success':
      alert(result.get('message', 'Add order failed'))
      return
    added = result.get('added') or []
    skipped = result.get('skipped') or []
    note = f"{order}: {len(added)} line(s) added"
    if skipped:
      note += f", {len(skipped)} already on the plan"
    Notification(note, timeout=4).show()
    if result.get('over_capacity'):
      # 066: the lines just added are re-read from the plan (the server
      # returns labels); the dialog acts on every active line of the order
      self.load_day()
      keys = [(l['nx_rel_numb'], l['nx_item_no']) for l in self._lines
              if l['nx_order_numb'] == order
              and l['line_status'] in ('Planned', 'Proposed', 'Confirmed')]
      self._cap_hit_dialog(order, keys, result)
    self.load_day()

  def _cap_hit_dialog(self, order, keys, result):
    """DECISION §4: snooze / Mark Stretch / explicit Unplaced / keep —
    never a silent drop. The line(s) are already added when we get here.
    Mark Stretch re-tiers them so they stop counting against the cap —
    "if capacity permits" made literal. 066: keys = [(rel, item), ...]."""
    sug = result.get('suggested_day')
    what = (order if len(keys) != 1
            else line_label({'nx_order_numb': order, 'nx_rel_numb': keys[0][0],
                             'nx_item_no': keys[0][1]}, with_sku=False))
    msg = (f"This puts {self.plan_date} at "
           f"{result['planned_inches']:,.0f} / "
           f"{result['capacity_inches']:,.0f} work-inches "
           f"(+{result['line_inches']:,.0f} from {what}).")
    buttons = []
    if sug:
      buttons.append((f"Snooze to {sug}", 'snooze'))
    buttons += [("Mark If time", 'stretch'),
                ("Leave Unplaced", 'unplaced'), ("Keep anyway", 'keep')]
    choice = alert(msg, title="Over capacity", buttons=buttons, large=True)
    for rel, item in keys:
      if choice == 'snooze' and sug:
        r = anvil.server.call('ship_plan_snooze_line', self.plan_date,
                              order, rel, item, sug)
      elif choice == 'stretch':
        r = anvil.server.call('ship_plan_set_priority', self.plan_date,
                              order, rel, item, tier='Stretch')
      elif choice == 'unplaced':
        r = anvil.server.call('ship_plan_mark_unplaced', self.plan_date,
                              order, rel, item, 'over capacity at add')
      else:
        return  # 'keep' (or dismissed): stays Planned, bar red — PC's call
      if r.get('status') != 'success':
        alert(r.get('message', 'Action failed'))
        return

  def _priority_dialog(self, item):
    """Tier + reason on one line. The REASON list is sales' vocabulary
    (pc_priority_reasons); the TIER (Must / Next / Stretch) is the
    Daily's call — sales proposes why, PC decides when."""
    if not hasattr(self, '_reasons'):
      r = anvil.server.call('ship_plan_get_reasons')
      ok = r.get('status') == 'success'
      self._reasons = (r.get('reasons') or []) if ok else []
    dd_tier = DropDown(items=[('Priority', 'Must'), ('Next', 'Next'),
                              ('If time — if capacity permits', 'Stretch')],
                       selected_value=item.get('tier') or 'Next')
    dd_reason = DropDown(
      items=[('— no reason set —', '')] +
      [(x['description'], x['reason_code']) for x in self._reasons],
      selected_value=item.get('priority_reason') or '')
    tb_note = TextBox(text=item.get('priority_note') or '',
                      placeholder='note — required for "Other"')
    pnl = ColumnPanel()
    pnl.add_component(Label(text='Tier', bold=True))
    pnl.add_component(dd_tier)
    pnl.add_component(Label(text='Reason (why it matters)', bold=True))
    pnl.add_component(dd_reason)
    pnl.add_component(Label(text='Note', bold=True))
    pnl.add_component(tb_note)
    if not confirm(f"Priority — {line_label(item)}",
                   content=pnl):
      return
    if (dd_reason.selected_value == 'OTHER'
        and not (tb_note.text or '').strip()):
      alert('"Other (see note)" needs the note — that IS the reason. '
            'Nothing saved; open ★ again.')
      return
    r = anvil.server.call('ship_plan_set_priority', self.plan_date,
                          item['nx_order_numb'], item['nx_rel_numb'],
                          item['nx_item_no'],
                          tier=dd_tier.selected_value,
                          reason=dd_reason.selected_value,
                          note=tb_note.text or '')
    if r.get('status') != 'success':
      alert(r.get('message', 'Priority change failed'))
    self.load_day()

  def _recalc_line(self, item):
    r = anvil.server.call('ship_plan_recalc_line', self.plan_date,
                          item['nx_order_numb'], item['nx_rel_numb'],
                          item['nx_item_no'])
    if r.get('status') != 'success':
      alert(r.get('message', 'Refresh failed'))
    else:
      Notification(f"Re-totaled: {r['old']} → {r['new']}",
                   timeout=4).show()
    self.load_day()

  def _snooze_line(self, item):
    tb = TextBox(text=item.get('snooze_suggestion') or '',
                 placeholder='YYYY-MM-DD')
    if not confirm(f"Snooze {line_label(item)} "
                   "to which day?", content=tb):
      return
    try:
      to_date = datetime.strptime((tb.text or '').strip(), '%Y-%m-%d').date()
    except ValueError:
      alert('Enter a date as YYYY-MM-DD.')
      return
    r = anvil.server.call('ship_plan_snooze_line', self.plan_date,
                          item['nx_order_numb'], item['nx_rel_numb'],
                          item['nx_item_no'], to_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'Snooze failed'))
    self.load_day()

  def _remove_line(self, item):
    order = item['nx_order_numb']
    siblings = [l for l in self._lines
                if l['nx_order_numb'] == order
                and l['line_status'] != 'Removed']
    tb = TextBox(placeholder='reason (optional)')
    cb_all = None
    content = tb
    if len(siblings) > 1:
      # Whole-order removal rides the same dialog — no extra button.
      cb_all = CheckBox(text=f'Remove ALL {len(siblings)} lines of {order}')
      content = ColumnPanel()
      content.add_component(cb_all)
      content.add_component(tb)
    if not confirm(f"Remove {line_label(item)} from this plan?",
                   content=content):
      return
    if cb_all and cb_all.checked:
      r = anvil.server.call('ship_plan_remove_order', self.plan_date,
                            order, tb.text or None)
    else:
      r = anvil.server.call('ship_plan_remove_line', self.plan_date,
                            order, item['nx_rel_numb'], item['nx_item_no'],
                            tb.text or None)
    if r.get('status') != 'success':
      alert(r.get('message', 'Remove failed'))
    self.load_day()

  def _partial_order_gate(self):
    """WHOLE-ORDER RULE (Dave, 2026-09-13): before Publish, list every
    order on the plan whose same-due-date lines are not all on it. One
    tap adds the rest (same tier as the order's planned lines); 'Publish
    anyway' is allowed and logged (PartialAck) so the retro can tell knew
    from missed. Returns (proceed, acknowledged_groups)."""
    r = anvil.server.call('ship_plan_partial_orders', self.plan_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'Partial-order check failed') +
            '\n\nPublishing without the check.')
      return True, []
    groups = r.get('groups') or []
    if not groups:
      return True, []
    lines_txt = []
    for g in groups:
      parts = []
      for l in g['lines']:
        s = f"{l.get('label') or ''} / {l['sku']} ({float(l['inches']):,.0f} in)"
        if l.get('elsewhere'):
          s += f" — {l['elsewhere']}"
        parts.append(s)
      lines_txt.append(f"Order {g['order']} (due {g['due_date']}, "
                       f"{g['tier']}): {len(g['lines'])} line(s) off the plan, "
                       f"{float(g['missing_inches']):,.0f} in\n    " + '\n    '.join(parts))
    n_addable = sum(g.get('n_addable', 0) for g in groups)
    msg = ("WHOLE-ORDER RULE — these orders are only partly on the plan. "
           "Assembly techs run each order as one job; a partial order on "
           "the board means it will not ship.\n\n" + '\n\n'.join(lines_txt))
    buttons = []
    if n_addable:
      buttons.append((f"Add the rest ({n_addable} line(s))", 'add'))
    buttons += [("Publish anyway", 'go'), ("Cancel", None)]
    choice = alert(msg, title='Partial orders on this plan', buttons=buttons,
                   large=True)
    if choice == 'add':
      n_ok = 0
      for g in groups:
        if not g.get('n_addable'):
          continue
        res = anvil.server.call('ship_plan_add_order', self.plan_date,
                                g['order'], g['tier'], None, g['due_date'])
        if res.get('status') == 'success':
          n_ok += len(res.get('added') or [])
        else:
          alert(f"{g['order']}: {res.get('message', 'add failed')}")
      Notification(f"{n_ok} line(s) added — review the plan, then Publish.",
                   timeout=5).show()
      return False, []
    if choice == 'go':
      return True, groups
    return False, []

  def _publish_click(self, **event_args):
    proceed, partial_ack = self._partial_order_gate()
    if not proceed:
      self.load_day()
      return
    cap = self._capacity or {}
    planned = sum(float(l['load_inches']) for l in self._lines
                  if l['line_status'] in ('Planned', 'Proposed', 'Confirmed'))
    # Publish gate (Dave 2026-08-25): a label can be missed at 8:50 —
    # a stale attendance snapshot gets its own line in the confirm.
    stale_line = ''
    if cap.get('snapshot_stale') and not cap.get('is_estimate'):
      stale_line = (f"⚠ CAPACITY IS BASED ON ATTENDANCE FROM "
                    f"{cap.get('snapshot_date')} — today's file has not "
                    "loaded. Publish anyway?\n\n")
    if not confirm(
      f"Publish {self.plan_date}?\n\n" + stale_line +
      f"Planned {planned:,.0f} of "
      f"{cap.get('capacity_inches_effective', 0):,.0f} work-inches.\n"
      f"{cap.get('capacity_detail', '')}\n\n"
      "The capacity number is stamped on the plan — it's the number "
      "sales sees."):
      return
    r = anvil.server.call('ship_plan_publish', self.plan_date, partial_ack)
    if r.get('status') != 'success':
      alert(r.get('message', 'Publish failed'))
    elif r.get('snapshot_warning'):
      # 051: the plan IS published; only the agent's history record failed.
      alert(r['snapshot_warning'] + '\n\nTell Dave — the plan itself is fine.')
    self.load_day()

  # =================================================================
  # Panels / history / timer
  # =================================================================
  def _params_click(self, **event_args):
    alert(ShipPlanParamsPanel(override_date=self.plan_date),
          title='Ship Plan parameters', large=True, buttons=[('Close', None)])
    self.load_day()

  def _overrides_click(self, **event_args):
    alert(ShipPlanParamsPanel(override_date=self.plan_date,
                              section='overrides'),
          title=f'Assembler overrides — {self.plan_date}',
          large=True, buttons=[('Close', None)])
    self.load_day()

  def _history_click(self, **event_args):
    r = anvil.server.call('ship_plan_get_events', self.plan_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'History load failed'))
      return
    lines = []
    for e in r['events']:
      what = ''
      if e['nx_order_numb']:
        what = (line_label(e) if e['nx_sku']
                else e['nx_order_numb'])   # order-level events have no SKU
      change = ''
      if e['old_value'] and e['new_value']:
        change = f": {e['old_value']} → {e['new_value']}"
      elif e['new_value']:
        change = f": {e['new_value']}"
      lines.append(f"{str(e['event_time'])[11:16]}  {e['event_type']:<11} "
                   f"{what}{change}  ({e['actor']})")
    ta = TextArea(text='\n'.join(lines) or 'No events yet.', height=400)
    ta.enabled = False
    alert(ta, title=f'Plan history — {self.plan_date}', large=True,
          buttons=[('Close', None)])

  def _timer_tick(self, **event_args):
    """READ-ONLY refresh: drift badges + the bar's planned total only.
    Never calls recalc (drift rule)."""
    result = anvil.server.call_s('ship_plan_get_plan', self.plan_date)
    if result.get('status') != 'success':
      return
    self._capacity = result['capacity']
    self._render_capacity(result['planned_inches'],
                          result.get('stretch_inches', 0))
    self._render_lines(result['lines'])

  # =================================================================
  # AI agent — proposal (051/052). The agent is another proposer: its
  # lines arrive as Proposed; PC confirms (per line or all) or removes.
  # =================================================================
  def _render_proposal(self, plan):
    self._proposal = plan.get('proposal')
    n_open = int(plan.get('n_proposed_open') or 0)
    if hasattr(self, 'lk_accept_all'):
      self.lk_accept_all.visible = self.is_pc and n_open > 0
      self.lk_accept_all.text = f'Accept all {n_open} proposed'
    if hasattr(self, 'lk_proposal'):
      p = self._proposal
      if p:
        when = (p.get('generated_at') or '')[11:16]
        self.lk_proposal.text = (f"Agent proposal {when} · {p.get('n_lines', 0)} lines · "
                                 f"{p.get('status', '')}")
        self.lk_proposal.visible = True
      else:
        self.lk_proposal.text = 'No agent proposal yet'
        self.lk_proposal.visible = True

  @staticmethod
  def _pline(text, size=15, bold=False, color=None):
    lbl = Label(text=text, font_size=size, bold=bold,
                spacing_above='none', spacing_below='none')
    if color:
      lbl.foreground = color
    return lbl

  def _show_proposal(self, **event_args):
    r = anvil.server.call('ship_plan_get_proposal', self.plan_date)
    if r.get('status') != 'success' or not r.get('proposal'):
      alert('No agent proposal for this day yet.')
      return
    p, lines = r['proposal'], r['lines']
    panel = ColumnPanel(spacing_above='none', spacing_below='none')
    panel.add_component(self._pline(
      f"Agent proposal — {self.plan_date}  ·  {p.get('status')}", size=17, bold=True))
    panel.add_component(self._pline(
      f"Proposed {float(p.get('proposed_inches') or 0):,.0f} of "
      f"{float(p.get('capacity_inches') or 0):,.0f} work-inches "
      f"(generated {(p.get('generated_at') or '')[:16]}, {p.get('agent_version') or ''})",
      color='#595959'))
    if p.get('rationale'):
      panel.add_component(self._pline(' ', size=6))
      for para in str(p['rationale']).split('\n'):
        if para.strip():
          panel.add_component(self._pline(para.strip(), size=14))
    else:
      panel.add_component(self._pline(
        'Narrative not written yet (the agent service adds it after the run).',
        size=13, color='#8a8a8a'))
    panel.add_component(self._pline(' ', size=6))
    panel.add_component(self._pline('Lines', size=15, bold=True))
    for l in lines:
      if int(l.get('rank_no') or 0) == 0:
        txt = f"+ {line_label(l)} — added by PC"
      else:
        txt = (f"{l['rank_no']}. {line_label(l)} — "
               f"{l.get('proposed_tier')} · {l.get('reason') or ''}")
      out = l.get('outcome')
      if out:
        txt += f"   [{out}]"
      panel.add_component(self._pline(txt, size=13,
                                      color='#8a8a8a' if out == 'DROPPED' else None))
    alert(content=panel, title='Agent proposal', buttons=[('Close', None)], large=True)

  def _confirm_line(self, item):
    r = anvil.server.call('ship_plan_confirm_line', self.plan_date,
                          item['nx_order_numb'], item['nx_rel_numb'],
                          item['nx_item_no'])
    if r.get('status') != 'success':
      alert(r.get('message', 'Confirm failed'))
    self.load_day()

  def _accept_all_proposed(self, **event_args):
    n = int((self._proposal or {}).get('n_lines') or 0)
    if not confirm(f'Accept every Proposed line on {self.plan_date}? '
                   'Each becomes Planned with the tier the agent set.'):
      return
    r = anvil.server.call('ship_plan_confirm_all_proposed', self.plan_date)
    if r.get('status') != 'success':
      alert(r.get('message', 'Accept-all failed'))
    self.load_day()

  def _run_agent_now(self, **event_args):
    """Manual trigger (PC/Dave) — the same deterministic fill the 06:30
    service run performs; useful before the service is scheduled and for
    a re-run after a big feed change. Supersedes the earlier proposal but
    keeps anything PC already confirmed."""
    if getattr(self, '_agent_task_id', None):
      alert('The agent is already running for this form — give it a moment.')
      return
    if not confirm(f'Run the agent for {self.plan_date} now? '
                   'Its earlier still-Proposed lines are replaced; '
                   'confirmed lines are kept.'):
      return
    r = anvil.server.call('ship_plan_agent_propose', self.plan_date)
    if r.get('status') != 'started':
      alert(r.get('message', 'Could not start the agent'))
      return
    # The fill runs as a BACKGROUND TASK (past the 30 s call limit);
    # poll every 2 s from a code-made Timer, then show the summary.
    self._agent_task_id = r['task_id']
    self._agent_polls = 0
    if hasattr(self, 'lk_run_agent'):
      self.lk_run_agent.text = 'Agent running…'
    self._agent_timer = Timer(interval=2)
    self._agent_timer.set_event_handler('tick', self._agent_poll)
    self.add_component(self._agent_timer)

  def _agent_poll(self, **event_args):
    self._agent_polls += 1
    s = anvil.server.call('ship_plan_agent_task_status', self._agent_task_id)
    if s.get('status') == 'running' and self._agent_polls < 150:   # 5 min cap
      return
    self._agent_timer.interval = 0
    self._agent_timer.remove_from_parent()
    self._agent_task_id = None
    if hasattr(self, 'lk_run_agent'):
      self.lk_run_agent.text = 'Run agent'
    if s.get('status') == 'running':
      alert('The agent is still running after 5 minutes — check the '
            'Background Tasks log in Anvil.')
      return
    if s.get('status') == 'error':
      alert(s.get('message', 'Agent run failed'))
      self.load_day()
      return
    r = s.get('result') or {}
    if r.get('status') == 'error':
      alert(r.get('message', 'Agent run failed'))
    elif r.get('status') == 'skipped':
      alert(r.get('message', 'Skipped'))
    else:
      msg = (f"Proposed {r.get('n_proposed', 0)} lines + {r.get('n_stretch', 0)} if-time — "
             f"{r.get('proposed_inches', 0):,.0f} of {r.get('target_inches', 0):,.0f} "
             f"target in ({int(float(r.get('fill_pct', 0.8)) * 100)}% of "
             f"{r.get('capacity_inches', 0):,.0f}). "
             f"{r.get('n_gated', 0)} gated, {r.get('n_skipped', 0)} beyond if-time, "
             f"{r.get('n_split_needed', 0)} need a split decision (bigger than a day "
             f"or than {r.get('max_line_inches', 0):,.0f} in), "
             f"{r.get('n_zero_touch_skipped', 0)} zero-touch left off.")
      if r.get('snapshot_warning'):
        msg += f"\n\nSnapshot warning: {r['snapshot_warning']}"
      alert(msg)
    self.load_day()