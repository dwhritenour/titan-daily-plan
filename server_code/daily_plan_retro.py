"""
Anvil Server Module - Daily Plan Retro-Review (v1, 2026-09-07)
===============================================================
The back half of the AI Agent Operating Cycle: read the previous business
day's PUBLISHED plan, decide what happened to every planned line from the
records Nexus already holds, write the variance ledger, and ask at most
RETRO_MAX_QUESTIONS bounded, factual questions (2 PC + 1 Sales floor).

Spec: documents/Retro_v1_Spec_2026-09-07.md. Schema: migrations 051 + 053.

What the records can prove (no Assembly App yet, kiosk Start/Complete
events unused since 08-21):
    SHIPPED   line gone from the open book, or balance below the
              published qty (sales_orders_due, hourly). Item status 15
              ('Material Received/Shipped') is deliberately NOT used: the
              status at publish is not snapshotted yet, and 86 lines sat
              at 15 before the first plan existed (2026-09-07 test).
    WORKED    a work order for the line completed since the plan day, or
              a floor tap (Start/Restart/Stop/Complete/Count) on the plan day
              - NOT an Assign (that is the Supervisor board, retro-1.7.1)
    REMOVED   plan event Removed / TierChange after publish - with a reason
              it is a variance with the cause attached (counts toward the
              N=3 recurrence); without one it earns a question
    UNTOUCHED none of the above (Must/Next earn a PC question; Stretch is
              optional by definition and closes silently)
    DATE_MOVED    ship-by on the open line != ship-by at publish, or a
              due_date change in work_order_changes (old -> new, when)
              -> Sales. Suppresses the PC question on the same line.
    CANCELLED     SO_Canceled / SO_Removed in work_order_changes since
              publish: the line left the open book but did NOT ship
              (v1.2, 2026-09-08 - before this it counted as SHIPPED)
    QTY_CHANGED   quantity FIELD_CHANGE since publish, no SO_Shipped: the
              balance dropped because Order Entry changed it, not a ship
    Evidence (v1.2): dbo.work_order_changes - the EM job diffs every work
    order against its sales-order line every 15 min (SO_Shipped with the
    time, SO_Canceled, SO_Removed, FIELD_CHANGE due_date / quantity).
    UNPLANNED_SHIP shipped on the day, not planned, load >= threshold -> Sales
Every inferred outcome enters the ledger as evidence 'Probable' at best;
'Verified' needs a person (Dave confirms the reading - always Dave).

Callables (the #retro and #retrev forms):
    retro_get_day(plan_date)                 header + all variances + day list
    retro_get_review()                       Dave's queue across days + recent + causes
    retro_answer(variance_id, answer, who)   free text, verbatim
    retro_confirm(variance_id, reading, evidence_class, cause_key,
                  resolution, pin)           Dave only (RETRO_ARBITER_PIN secret)
    retro_ask_anyway(variance_id)            promote a 'not asked today' row
    retro_run(plan_date=None, force=False)   manual run from the app
HTTP (titan-ai-service, X-Agent-Key): POST /agent/retro {plan_date, force}

Identity note (v1): the app has no user login (events show actor
'Production'), so the form asks WHO is answering from the RETRO_*_OWNERS
lists and the confirm step takes Dave's PIN. Real identity comes with the
app-wide role work.

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
"""

import anvil.server
import anvil.secrets
import re
from datetime import datetime, date, timedelta

from .daily_ship_plan import (get_connection, _parse_date, _current_actor,
_get_params, _agent_key_ok, serialize_row,
make_serializable)

_RETRO_VERSION = 'retro-1.7.1'   # 1.7.1: Assign is not work; 1.7 (117): multi-day lines from the tablet; 1.6 (097): stall rule, one question per order; 1.5 (067): actual crew/capacity; 1.4 (066): e21-line grain
_WO_DONE = ('Complete', 'Shipped - Verify', 'Closed - Verify')

# retro-1.7 (2026-10-25, multi-day paper s13 / decisions 1 and 4): a planned
# line whose board note starts 'SPLIT n of N · day d of about k' (a day-sized
# share, agent v1.5) or 'MULTI-DAY:' (a share planned on an earlier day) is
# read from the Assembly tablet (dbo.work_order_events) instead of the
# shipped / worked / untouched trio:
#   FINISHED  Complete tapped (since the plan was published, or earlier)
#   ON_TRACK  a Count today at or above half the share, or any floor tap today
#             with no count recorded
#   SLOW      a Count today under half the share (a question when it repeats)
#   PAUSED    the last tap is Stop (no Restart) - no question
#   STOPPED   started on the tablet, no tap today; stalls after
#             MULTIDAY_STALL_DAYS published plan days without a tap (2)
# A share line the tablet never started stays UNTOUCHED (the 3-day stall rule).
# The Count tap is event_type 'Count', note 'pieces=<n>' (migration 118, the
# tablet app writes it); until it exists ON_TRACK means 'tapped today'.
_MD_SHARE_LIKE = 'SPLIT % of % · day %'
_MD_BUILT_LIKE = 'MULTI-DAY: %'
_MD_FLOOR_TAPS = ('Start', 'Restart', 'Stop', 'Complete', 'Count')   # Assign is the board, not the floor


# =============================================================================
# HELPERS
# =============================================================================

def _lk(s):
  """Line key of a snapshot / plan / variance row: (order, rel, item).
  Pre-066 rows carry rel/item = '' - see _look()."""
  return (s['nx_order_numb'], (s.get('nx_rel_numb') or ''), (s.get('nx_item_no') or ''))


def _look(by_item, by_sku, s):
  """Item-grain lookup with a (order, sku) fallback for pre-066 rows."""
  k = _lk(s)
  if k[2]:
    return by_item.get(k)
  return by_sku.get((s['nx_order_numb'], s['nx_sku']))


def _line_txt(s):
  """'595613-001-20 / YS61S0600' - order, release, item, always all three."""
  o, r, i = _lk(s)
  base = f"{o}-{r}-{i}" if (r or i) else f"{o}"
  return f"{base} / {s['nx_sku']}"


def _prev_business_day(d=None):
  d = (d or date.today()) - timedelta(days=1)
  while d.weekday() >= 5:
    d -= timedelta(days=1)
  return d


def _retro_params(cursor):
  p = _get_params(cursor)
  def _i(k, d):
    try:
      return int(float(p.get(k, d)))
    except (TypeError, ValueError):
      return int(d)
  def _names(k):
    return [s.strip() for s in str(p.get(k, '') or '').split(',') if s.strip()]
  return {'enabled': str(p.get('RETRO_ENABLED', '1')).strip() == '1',
          'max_q': max(0, _i('RETRO_MAX_QUESTIONS', 3)),
          'min_sales': max(0, _i('RETRO_MIN_SALES_QUESTIONS', 1)),
          'unplanned_min': float(p.get('RETRO_UNPLANNED_MIN_INCHES', 50) or 50),
          'unknown_days': max(1, _i('RETRO_UNKNOWN_AFTER_DAYS', 5)),
          # 096 stall rule: untouched this many published plan days in a row
          # -> off the plan (STALLED hold, X07) + one forced PC question.
          # 0 = rule off. STALL_HOLD_DAYS = how long the hold lasts with no
          # dated answer (business days).
          'stall_days': max(0, _i('STALL_DAYS', 3)),
          'stall_hold_days': max(1, _i('STALL_HOLD_DAYS', 5)),
          # retro-1.7 (decision 4): a STARTED multi-day line stalls this many
          # published plan days after its last floor tap (0 = off).
          'md_stall_days': max(0, _i('MULTIDAY_STALL_DAYS', 2)),
          'pc_owners': _names('RETRO_PC_OWNERS'),
          'sales_owners': _names('RETRO_SALES_OWNERS'),
          'fill_pct': max(0.0, min(float(p.get('AGENT_FILL_PCT', 80) or 80), 100.0)) / 100.0}


def _jsonable(v):
  if isinstance(v, dict):
    return {k: _jsonable(x) for k, x in v.items()}
  if isinstance(v, (list, tuple)):
    return [_jsonable(x) for x in v]
  return make_serializable(v)


def _fmt_day(d):
  return d.strftime('%a %m/%d')


def _role_of(actor, rp):
  a = (actor or '').lower()
  if any(n.lower() in a for n in rp['sales_owners']):
    return 'SALES'
  if any(n.lower() in a for n in rp['pc_owners']):
    return 'PC'
  if 'sales' in a:
    return 'SALES'
  return 'PC'          # PC owns the plan; unknown removers default here


def _owners(role, rp):
  names = rp['sales_owners'] if role == 'SALES' else rp['pc_owners']
  return ', '.join(names) if names else ('Sales' if role == 'SALES' else 'Production Control')


# =============================================================================
# 096 - the stall rule (decision 2, 2026-10-12)
# =============================================================================

def _md_plan_lines(cursor, plan_date):
  """retro-1.7: the day's multi-day lines from their board notes.
  {(order, rel, item): {'share': qty_planned, 'day': d, 'about': k,
                        'built': True when the note is 'MULTI-DAY:' (0 in)}}"""
  cursor.execute("""
      SELECT nx_order_numb, nx_rel_numb, nx_item_no, qty_planned, priority_note
      FROM dbo.pc_ship_plan_line
      WHERE plan_date = %s AND line_status IN ('Planned', 'Proposed', 'Confirmed')
        AND (priority_note LIKE %s OR priority_note LIKE %s)
  """, (plan_date, _MD_SHARE_LIKE, _MD_BUILT_LIKE))
  out = {}
  for r in cursor.fetchall():
    note = r['priority_note'] or ''
    m = re.search(r' · day (\d+) of about (\d+)', note)
    out[(r['nx_order_numb'], r['nx_rel_numb'] or '', str(r['nx_item_no'] or ''))] = {
      'share': float(r['qty_planned'] or 0),
      'day': int(m.group(1)) if m else None, 'about': int(m.group(2)) if m else None,
      'built': note.startswith('MULTI-DAY:')}
  return out


def _md_count(note):
  """pieces from a Count tap's note ('pieces=120'; a bare number also counts)."""
  m = re.search(r'pieces\s*=\s*(\d+)', note or '', re.I) or re.match(r'\s*(\d+)\s*$', note or '')
  return int(m.group(1)) if m else None


def _md_tablet(cursor, plan_date, days_back=30):
  """retro-1.7: what the Assembly tablet says per e21 line, from the floor
  taps of the last `days_back` days up to the end of plan_date:
  {(order, rel, item): {'state': running | paused | done | None,
                        'last_tap': date or None, 'tapped_today': bool,
                        'done_at': datetime or None,
                        'count_today': int or None, 'count_total': int,
                        'first_start': date or None}}.
  Assign (the Supervisor board) is not a floor tap. Several work orders on
  one line (rework) fold into one record, newest tap wins the state."""
  cursor.execute("""
      SELECT wo.sales_order_number AS o, wo.sales_order_rel AS rel,
             CAST(wo.sales_order_line AS VARCHAR(10)) AS item,
             e.event_type, e.event_at, e.note
      FROM dbo.work_order_events e
      JOIN dbo.work_orders wo ON wo.work_order_id = e.work_order_id
      WHERE wo.status <> 'Cancelled'
        AND e.event_type IN %s
        AND e.event_at >= DATEADD(day, -%s, %s)
        AND e.event_at < DATEADD(day, 1, %s)
      ORDER BY e.event_at, e.event_id
  """, (_MD_FLOOR_TAPS, int(days_back), plan_date, plan_date))
  out = {}
  for e in cursor.fetchall():
    k = (e['o'], e['rel'] or '', e['item'] or '')
    t = out.setdefault(k, {'state': None, 'last_tap': None, 'tapped_today': False,
                           'done_at': None, 'count_today': None, 'count_total': 0,
                           'first_start': None})
    at = e['event_at']
    d = at.date() if isinstance(at, datetime) else at
    typ = e['event_type']
    t['last_tap'] = d
    if d == plan_date:
      t['tapped_today'] = True
    if typ in ('Start', 'Restart'):
      t['state'] = 'running'
      t['first_start'] = t['first_start'] or d
    elif typ == 'Stop':
      t['state'] = 'paused'
    elif typ == 'Complete':
      t['state'] = 'done'
      t['done_at'] = at
    elif typ == 'Count':
      n = _md_count(e['note'])
      if n is not None:
        t['count_total'] += n
        if d == plan_date:
          t['count_today'] = (t['count_today'] or 0) + n
      if t['state'] is None:
        t['state'] = 'running'          # a count is proof of work even without a Start
  return out


def _md_stall_scan(cursor, plan_date, rows, rp):
  """retro-1.7 (decision 4): a STOPPED multi-day line - started on the
  tablet, no floor tap today - stalls when the published plan days since
  its last tap reach MULTIDAY_STALL_DAYS (2). Same hold as the 3-day rule
  (STALLED, X07), its own question. Returns the stalled rows."""
  n = rp.get('md_stall_days') or 0
  if n <= 0:
    return []
  cursor.execute("""
      SELECT TOP (12) plan_date FROM dbo.pc_plan_retro
      WHERE plan_date < %s ORDER BY plan_date DESC
  """, (plan_date,))
  prior_days = [r['plan_date'] for r in cursor.fetchall()]
  stalled = []
  for r in rows:
    if r.get('case') != 'STOPPED' or not r.get('md_last_tap'):
      continue
    last = r['md_last_tap']
    days = [d for d in [plan_date] + prior_days if d > last]
    if len(days) < n:
      continue
    cursor.execute("""
        SELECT TOP 1 hold_id FROM dbo.pc_plan_line_hold
        WHERE nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND released_at IS NULL AND expires_on >= CAST(GETDATE() AS DATE)
    """, (r['order'], r.get('rel'), r.get('item')))
    if cursor.fetchone():
      continue
    r['stall_days'] = len(days)
    r['stall_first'] = min(days)
    r['routed_to'] = 'PC'
    r['question'] = (f"Multi-day order {r.get('line') or r['order']} (day {r.get('md_day') or '?'} of about "
                     f"{r.get('md_about') or '?'}) was started on the tablet on {last:%m/%d} and has had "
                     f"no tap for {len(days)} plan days. It has been taken off the plan. Is it still being "
                     "built, paused, or blocked - and on what date does it continue? It comes back on that "
                     "date, or sooner if PC releases the hold.")
    r['explanation'] = (r.get('explanation') or '') + f"; STALLED - {len(days)} plan days since the last tap"
    stalled.append(r)
  return stalled


def _stall_streaks(cursor, plan_date, max_days=12):
  """For every line UNTOUCHED on plan_date (in-memory rows, not yet
  written), how many published plan days IN A ROW ending on plan_date it
  has been untouched. Walks the prior retro days (pc_plan_retro = the days
  that were published and reviewed) and the variance ledger. Returns
  {(order, rel, item): [plan_dates...]} for the prior days only; the caller
  adds today."""
  cursor.execute("""
      SELECT TOP (%s) plan_date FROM dbo.pc_plan_retro
      WHERE plan_date < %s ORDER BY plan_date DESC
  """, (max_days, plan_date))
  prior_days = [r['plan_date'] for r in cursor.fetchall()]
  if not prior_days:
    return {}, []
  cursor.execute("""
      SELECT plan_date, nx_order_numb, nx_rel_numb, nx_item_no
      FROM dbo.pc_plan_variance
      WHERE plan_date >= %s AND plan_date < %s
        AND variance_case = 'UNTOUCHED' AND expected = 'WORKED_ON'
  """, (prior_days[-1], plan_date))
  untouched = {}
  for r in cursor.fetchall():
    untouched.setdefault((r['nx_order_numb'], r['nx_rel_numb'] or '', r['nx_item_no'] or ''),
                         set()).add(r['plan_date'])
  return untouched, prior_days


def _stall_scan(cursor, plan_date, rows, rp):
  """Mark the rows that stall today. A row stalls when it is UNTOUCHED
  (routed to PC - a Sales date move is not a stall), its streak reaches
  STALL_DAYS, and the line has no live hold already. The forced question
  replaces the usual 'was it started?' and is ALWAYS sent, on top of
  RETRO_MAX_QUESTIONS. Returns the stalled rows (hold placed later, once
  the variance row exists and has an id)."""
  n = rp.get('stall_days') or 0
  if n <= 0:
    return []
  untouched, prior_days = _stall_streaks(cursor, plan_date)
  stalled = []
  for r in rows:
    if r.get('case') != 'UNTOUCHED' or r.get('routed_to') != 'PC' or r.get('expected') != 'WORKED_ON':
      continue
    key = (r['order'], r.get('rel') or '', r.get('item') or '')
    days = [plan_date]
    for d in prior_days:                      # newest first: consecutive only
      if d in untouched.get(key, ()):
        days.append(d)
      else:
        break
    if len(days) < n:
      continue
    cursor.execute("""
        SELECT TOP 1 hold_id, hold_kind, expires_on FROM dbo.pc_plan_line_hold
        WHERE nx_order_numb = %s AND nx_rel_numb = %s AND nx_item_no = %s
          AND released_at IS NULL AND expires_on >= CAST(GETDATE() AS DATE)
    """, (r['order'], r.get('rel'), r.get('item')))
    if cursor.fetchone():
      continue                                # already held (material or stalled)
    first, last = min(days), max(days)
    r['stall_days'] = len(days)
    r['stall_first'] = first
    r['question'] = (f"Order {r.get('line') or r['order']} has been on the plan {len(days)} days "
                     f"in a row ({first:%m/%d} to {last:%m/%d}) with no shipment or work-order "
                     "activity, and has been taken off the plan. What is it waiting on, and on "
                     "what date can it be built? It comes back on that date, or sooner if PC "
                     "releases the hold.")
    r['explanation'] = (r.get('explanation') or '') + f"; STALLED after {len(days)} days - off the plan"
    stalled.append(r)
  # one question per ORDER, not per line (605989 has nine lines): the first
  # line of the order carries the question and names the others; the rest
  # still stall (hold + ledger row) but are not asked separately.
  by_order = {}
  for r in stalled:
    by_order.setdefault(r['order'], []).append(r)
  for order, group in by_order.items():
    if len(group) < 2:
      continue
    group.sort(key=lambda x: (x.get('rel') or '', int(x.get('item') or 0) if str(x.get('item') or '').isdigit() else 0))
    lead, rest = group[0], group[1:]
    others = ', '.join(str(x.get('item') or '?') for x in rest)
    lead['question'] = (f"Order {order}: {len(group)} lines (items {lead.get('item') or '?'}, {others}) "
                        f"have been on the plan {lead['stall_days']} days in a row with no shipment or "
                        "work-order activity, and have been taken off the plan. What is the order waiting "
                        "on, and on what date can it be built? The lines come back on that date, or "
                        "sooner if PC releases the holds.")
    for x in rest:
      x['question'] = None            # stalls (hold), not asked separately
      x['asked_to'] = None
      x['stall_lead'] = lead          # its hold is tied to the lead's answer
  return stalled


def _place_stall_holds(cursor, plan_date, stalled, rp):
  """After the ledger write: one STALLED hold per stalled line (X07 gate,
  sp_pc_place_line_hold with @hold_kind = 'STALLED'), tied to the day's
  variance row so the PC answer can set its expiry. Never raises."""
  placed = []
  for r in stalled:
    try:
      src = r.get('stall_lead') or r       # sibling holds answer with the order's lead line
      cursor.execute("""
          SELECT TOP 1 variance_id FROM dbo.pc_plan_variance
          WHERE plan_date = %s AND nx_order_numb = %s AND ISNULL(nx_rel_numb, '') = %s
            AND ISNULL(nx_item_no, '') = %s AND expected = 'WORKED_ON'
      """, (plan_date, src['order'], src.get('rel') or '', src.get('item') or ''))
      v = cursor.fetchone()
      reason = (f"Stalled: {r['stall_days']} plan days with no activity "
                f"({r['stall_first']:%m/%d} to {plan_date:%m/%d}); off the plan until PC gives a date")
      cursor.execute("""
          EXEC dbo.sp_pc_place_line_hold @order = %s, @rel = %s, @item = %s,
               @reason = %s, @placed_by = %s, @source_variance_id = %s,
               @days = %s, @hold_kind = 'STALLED'
      """, (r['order'], r.get('rel'), r.get('item'), reason[:500], 'agent (stall rule)',
            (v['variance_id'] if v else None), rp['stall_hold_days']))
      h = cursor.fetchone() or {}
      placed.append({'line': r.get('line') or r['order'], 'days': r['stall_days'],
                     'expires_on': h.get('expires_on'), 'n_unplaced': int(h.get('n_unplaced') or 0)})
    except Exception as e:
      placed.append({'line': r.get('line') or r['order'], 'days': r['stall_days'],
                     'error': str(e)[:200]})
  return placed


# =============================================================================
# THE MORNING RUN
# =============================================================================

def _retro_run_impl(plan_date=None, force=False, actor=None):
  """Review one published day. Idempotent: unanswered rows for the day are
  rebuilt; answered rows and the header's narrative survive a rerun."""
  actor = actor or _current_actor()
  plan_date = _parse_date(plan_date) if plan_date else _prev_business_day()
  conn = get_connection()
  try:
    cursor = conn.cursor()
    rp = _retro_params(cursor)
    if not rp['enabled'] and not force:
      return {'status': 'skipped', 'message': 'RETRO_ENABLED is 0.'}

    # --- the published snapshot = what we said we'd do -----------------
    cursor.execute("""
        SELECT MAX(revision_no) AS rev FROM dbo.pc_plan_flag_snapshot
        WHERE plan_date = %s AND revision_no >= 0
    """, (plan_date,))
    rev = (cursor.fetchone() or {}).get('rev')
    if rev is None:
      return {'status': 'skipped', 'plan_date': plan_date.isoformat(),
              'message': f'{plan_date} has no published snapshot - nothing to review.'}
    cursor.execute("""
        SELECT plan_status, capacity_inches, published_at FROM dbo.pc_ship_plan
        WHERE plan_date = %s
    """, (plan_date,))
    hdr = cursor.fetchone() or {}
    published_at = hdr.get('published_at')
    cap_in = float(hdr.get('capacity_inches') or 0)
    reserve_in = round(cap_in * (1.0 - rp['fill_pct']), 2) if cap_in else None

    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, was_planned, tier,
               is_gated, priority_score, flag_codes, ship_by_date, qty_open, load_inches
        FROM dbo.pc_plan_flag_snapshot
        WHERE plan_date = %s AND revision_no = %s
    """, (plan_date, rev))
    snap = cursor.fetchall()
    planned = [s for s in snap if s['was_planned']]
    unplanned_pool = [s for s in snap if not s['was_planned']]

    # v1.2.1 (Dave, 2026-09-08): plan POSITION = the order the floor saw.
    # Same ordering as sp_stamp_plan_work_orders (tier, position, load DESC)
    # so the first retro question is about the line released first.
    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku,
               ROW_NUMBER() OVER (ORDER BY CASE tier WHEN 'Must' THEN 0
                                                     WHEN 'Next' THEN 1 ELSE 2 END,
                                           position, load_inches DESC) AS pos
        FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND line_status IN ('Planned', 'Proposed', 'Confirmed')
    """, (plan_date,))
    plan_pos, plan_pos_sku = {}, {}
    for r in cursor.fetchall():
      plan_pos[_lk(r)] = int(r['pos'])
      plan_pos_sku.setdefault((r['nx_order_numb'], r['nx_sku']), int(r['pos']))

    # --- what the records say now -------------------------------------
    # 066: one row per e21 line; "balance" = the ORDERED quantity (orditem.qty),
    # the same basis as the snapshot's qty_open. An (order, sku) aggregate
    # serves pre-066 snapshot rows.
    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku,
               ISNULL(nx_qty_ordered, nx_qty_balance_due) AS balance,
               nx_item_status AS item_status,
               CAST(COALESCE(nx_dship_by_date, nx_hship_by_date) AS DATE) AS ship_by
        FROM dbo.sales_orders_due
    """)
    book, book_sku = {}, {}
    for r in cursor.fetchall():
      book[_lk(r)] = r
      a = book_sku.get((r['nx_order_numb'], r['nx_sku']))
      if a is None:
        book_sku[(r['nx_order_numb'], r['nx_sku'])] = dict(r)
      else:
        a['balance'] = float(a['balance'] or 0) + float(r['balance'] or 0)
        a['item_status'] = max(a['item_status'] or '', r['item_status'] or '')
        if r['ship_by'] and (a['ship_by'] is None or r['ship_by'] < a['ship_by']):
          a['ship_by'] = r['ship_by']

    cursor.execute("""
        SELECT wo.sales_order_number AS o, wo.sales_order_rel AS rel,
               CAST(wo.sales_order_line AS VARCHAR(10)) AS item, wo.part_number AS s,
               MAX(CASE WHEN wo.status IN ('Complete','Shipped - Verify','Closed - Verify')
                         AND wo.completed_at >= %s THEN 1 ELSE 0 END) AS done_since,
               MAX(CASE WHEN e.event_id IS NOT NULL THEN 1 ELSE 0 END)   AS touched,
               MAX(wo.status) AS wo_status
        FROM dbo.work_orders wo
        LEFT JOIN dbo.work_order_events e
               ON e.work_order_id = wo.work_order_id
              AND e.event_type IN ('Start','Restart','Stop','Complete','Count')   -- 1.7.1: floor taps only; Assign is the Supervisor board (10-23: 36 lines read WORKED on bulk assigns)
              AND CAST(e.event_at AS DATE) = %s
        WHERE wo.status <> 'Cancelled'
        GROUP BY wo.sales_order_number, wo.sales_order_rel, wo.sales_order_line, wo.part_number
    """, (plan_date, plan_date))
    wos, wos_sku = {}, {}
    for r in cursor.fetchall():
      wos[(r['o'], r['rel'], r['item'])] = r
      a = wos_sku.get((r['o'], r['s']))
      if a is None:
        wos_sku[(r['o'], r['s'])] = dict(r)
      else:
        a['done_since'] = max(a['done_since'], r['done_since'])
        a['touched'] = max(a['touched'], r['touched'])

    # --- retro-1.7: the day's multi-day lines and what the tablet says ----
    md_lines = _md_plan_lines(cursor, plan_date)
    tablet = _md_tablet(cursor, plan_date) if md_lines else {}
    # yesterday's SLOW rows: a second SLOW day earns the question
    slow_before = set()
    if md_lines:
      cursor.execute("""
          SELECT nx_order_numb, nx_rel_numb, nx_item_no FROM dbo.pc_plan_variance
          WHERE plan_date = (SELECT MAX(plan_date) FROM dbo.pc_plan_retro WHERE plan_date < %s)
            AND variance_case = 'SLOW'
      """, (plan_date,))
      slow_before = {(r['nx_order_numb'], r['nx_rel_numb'] or '', str(r['nx_item_no'] or ''))
                     for r in cursor.fetchall()}

    # --- what Order Entry changed since publish (v1.2) -----------------
    # dbo.work_order_changes: the EM job diffs every work order against its
    # sales-order line every 15 min. Time-stamped evidence the open book
    # cannot give: SO_Shipped (when), SO_Canceled / SO_Removed (gone but
    # NOT shipped), FIELD_CHANGE due_date / quantity (old -> new, when).
    since = published_at or datetime.combine(plan_date, datetime.min.time())
    cursor.execute("""
        SELECT wo.sales_order_number AS o, wo.sales_order_rel AS rel,
               CAST(wo.sales_order_line AS VARCHAR(10)) AS item, wo.part_number AS s,
               c.change_type, c.field_name, c.old_value, c.new_value, c.detected_at
        FROM dbo.work_order_changes c
        JOIN dbo.work_orders wo ON wo.work_order_id = c.work_order_id
        WHERE c.detected_at >= %s
          AND (c.change_type IN ('SO_Shipped', 'SO_Canceled', 'SO_Removed')
               OR (c.change_type = 'FIELD_CHANGE'
                   AND c.field_name IN ('due_date', 'quantity')))
        ORDER BY c.detected_at
    """, (since,))
    changes, changes_sku = {}, {}
    for c in cursor.fetchall():
      changes.setdefault((c['o'], c['rel'], c['item']), []).append(c)
      changes_sku.setdefault((c['o'], c['s']), []).append(c)

    def _chg(s, *types, field=None):
      k = _lk(s)
      lst = changes.get(k, ()) if k[2] else changes_sku.get((s['nx_order_numb'], s['nx_sku']), ())
      return [c for c in lst
              if c['change_type'] in types and (field is None or c['field_name'] == field)]

    def _hm(dt):
      return dt.strftime('%H:%M') if dt else '?'

    cursor.execute("""
        SELECT nx_order_numb, nx_rel_numb, nx_item_no, nx_sku, event_type, actor, note,
               new_value, event_time
        FROM dbo.pc_plan_line_events
        WHERE plan_date = %s AND event_type IN ('Removed','TierChange','OrderRemoved')
          AND nx_order_numb IS NOT NULL
        ORDER BY event_time
    """, (plan_date,))
    removals, removals_sku = {}, {}
    for e in cursor.fetchall():
      if published_at and e['event_time'] and e['event_time'] < published_at:
        continue                       # pre-publish edits are the plan, not a variance
      if e.get('nx_item_no'):
        removals[_lk(e)] = e
      removals_sku[(e['nx_order_numb'], e['nx_sku'])] = e

    # --- classify every planned line ----------------------------------
    rows, counts = [], {'SHIPPED': 0, 'WORKED': 0, 'REMOVED_WITH_REASON': 0,
                        'REMOVED_NO_REASON': 0, 'UNTOUCHED': 0,
                        'UNTOUCHED_STRETCH': 0, 'DATE_MOVED': 0, 'UNPLANNED_SHIP': 0,
                        'CANCELLED': 0, 'QTY_CHANGED': 0, 'PARTIAL_ORDER': 0,
                        'FINISHED': 0, 'ON_TRACK': 0, 'SLOW': 0, 'PAUSED': 0, 'STOPPED': 0}   # retro-1.7
    planned_in = moved_in = 0.0
    n_stretch = 0
    for s in planned:
      key = (s['nx_order_numb'], s['nx_sku'])       # display / ledger key
      lkey = _lk(s)                                  # 066: the e21 line
      tier = s['tier'] or 'Next'
      load = float(s['load_inches'] or 0)
      qty0 = float(s['qty_open'] or 0)
      if tier == 'Stretch':
        n_stretch += 1
      else:
        planned_in += load
      b = _look(book, book_sku, s)
      w = _look(wos, wos_sku, s)
      bal = float(b['balance'] or 0) if b else 0.0
      # 2026-09-07 test: item status 15 (Material Received/Shipped) is NOT a
      # shipped signal by itself - 86 candidate lines sat at 15 before the
      # plan existed. Until the snapshot records the status at publish,
      # shipped = the line left the open book or its balance dropped.
      shp = _chg(s, 'SO_Shipped')
      cxl = _chg(s, 'SO_Canceled', 'SO_Removed')
      qch = _chg(s, 'FIELD_CHANGE', field='quantity')
      dch = _chg(s, 'FIELD_CHANGE', field='due_date')
      # v1.2: the change log outranks the open-book inference. A line that
      # left the book because Order Entry cancelled it did not ship; a
      # balance that dropped because the quantity was edited did not ship.
      cancelled = bool(cxl) and not shp
      qty_edited = bool(qch) and not shp and b is not None
      shipped = bool(shp) or ((b is None or bal < qty0 - 0.001)
                              and not cancelled and not qty_edited)
      worked = bool(w and (w['done_since'] or w['touched']))
      rem = removals.get(lkey) if lkey[2] else removals_sku.get(key)
      late = (s['ship_by_date'] and s['ship_by_date'] < plan_date)
      # date moved: the change log (old -> new, when) or the open book
      new_ship_by = None
      if dch:
        try:
          new_ship_by = date.fromisoformat((dch[-1]['new_value'] or '')[:10])
        except ValueError:
          new_ship_by = None
      if new_ship_by is None and b is not None and b['ship_by'] and s['ship_by_date'] \
      and b['ship_by'] != s['ship_by_date']:
        new_ship_by = b['ship_by']
      date_moved = bool(new_ship_by and s['ship_by_date'] and new_ship_by != s['ship_by_date'])
      base = (f"{tier} on {_fmt_day(plan_date)}, {load:,.0f} in"
              + (f", late since {s['ship_by_date']:%m-%d}" if late else '')
              + (f", flags {s['flag_codes']}" if s.get('flag_codes') else ''))
      row = {'order': key[0], 'sku': key[1], 'rel': lkey[1], 'item': lkey[2],
             'line': _line_txt(s), 'tier': tier, 'load': load,
             'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
             'pos': (plan_pos.get(lkey) if lkey[2] else plan_pos_sku.get(key)) or 10 ** 6,
             'expected': 'SHIPPED', 'expected_value': qty0,
             'actual_value': (None if b is None else bal),
             'observed': False, 'case': None, 'routed_to': None,
             'explanation': base, 'question': None, 'asked_to': None,
             'cause_key': None, 'resolution': 'OPEN', 'removed_by_role': None}
      if cancelled:
        c0 = cxl[-1]
        row.update(case='CANCELLED', observed=True, routed_to='SALES',
                   explanation=base + f"; {c0['change_type']} at {_hm(c0['detected_at'])} "
                   "per the work-order change log - left the book, did not ship",
                   question=(f"Order {_line_txt(s)} was {tier} on {_fmt_day(plan_date)} "
                             f"and was cancelled/removed at {_hm(c0['detected_at'])} after the plan "
                             "was published. Customer cancellation, or an entry change?"))
      elif shipped:
        row.update(case='SHIPPED', observed=True, resolution='EXPLAINED',
                   explanation=base + (f"; shipped {_hm(shp[-1]['detected_at'])} per the "
                                       "work-order change log" if shp else
                                       '; line left the open book' if b is None else
                                       f"; balance {qty0:,.0f} -> {bal:,.0f}"))
        if tier != 'Stretch':
          moved_in += load
      elif qty_edited:
        q0 = qch[-1]
        row.update(case='QTY_CHANGED', observed=True, routed_to='SALES',
                   explanation=base + f"; quantity {q0['old_value']} -> {q0['new_value']} at "
                   f"{_hm(q0['detected_at'])} per the work-order change log - not a shipment",
                   question=(f"Order {_line_txt(s)} quantity changed from {q0['old_value']} "
                             f"to {q0['new_value']} at {_hm(q0['detected_at'])} after "
                             f"{_fmt_day(plan_date)}'s plan was published. Customer change, "
                             "or an entry correction?"))
      elif lkey in md_lines:
        # retro-1.7: a multi-day share is read from the tablet (paper s13)
        md = md_lines[lkey]
        t = tablet.get(lkey) or {}
        dk = (f"day {md['day']} of about {md['about']}" if md.get('day') else 'multi-day')
        share = md['share']
        row['md_day'], row['md_about'] = md.get('day'), md.get('about')
        row['md_last_tap'] = t.get('last_tap')
        if t.get('state') == 'done' or (w and w['done_since']):
          when = t.get('done_at')
          row.update(case='FINISHED', expected='WORKED_ON', observed=True, resolution='EXPLAINED',
                     explanation=base + f"; {dk}: FINISHED - tablet Complete"
                     + (f" {when:%m/%d %H:%M}" if when else ' (work order done)')
                     + (f", {md['day']} of about {md['about']} days" if md.get('day') and md.get('about') else ''))
          if tier != 'Stretch':
            moved_in += load
        elif md.get('built'):
          # a share planned on an earlier day, riding at 0 in until the order ships
          row.update(case='WORKED', expected='WORKED_ON', observed=bool(t.get('first_start')),
                     resolution='EXPLAINED',
                     explanation=base + '; multi-day: share planned on an earlier day, awaiting the order'
                     + (f" (tablet: {t.get('state')}, last tap {t['last_tap']:%m/%d})" if t.get('last_tap') else ' (no tablet record)'))
        elif t.get('count_today') is not None:
          n = int(t['count_today'])
          row.update(expected='QTY_COMPLETED', expected_value=share, actual_value=float(n), observed=True)
          if share <= 0 or n >= 0.5 * share:
            row.update(case='ON_TRACK', resolution='EXPLAINED',
                       explanation=base + f"; {dk}: ON TRACK - {n:,} of {share:,.0f} counted today")
          else:
            repeat = lkey in slow_before
            row.update(case='SLOW', routed_to='PC', resolution=('OPEN' if repeat else 'EXPLAINED'),
                       explanation=base + f"; {dk}: SLOW - {n:,} of {share:,.0f} counted today"
                       + (' (second day running)' if repeat else ' (first day - not asked)'),
                       question=(f"Multi-day order {_line_txt(s)} ({dk}) counted {n:,} pieces today "
                                 f"against a share of {share:,.0f}, and was under half its share yesterday "
                                 "too. What is slowing it - and should the share, the assembler count, "
                                 "or the day estimate change?") if repeat else None)
          if tier != 'Stretch':
            moved_in += load * (min(1.0, n / share) if share > 0 else 1.0)
        elif t.get('state') == 'paused':
          row.update(case='PAUSED', expected='WORKED_ON', observed=True, resolution='EXPLAINED',
                     explanation=base + f"; {dk}: PAUSED - tablet Stop on {t['last_tap']:%m/%d}, no Restart")
        elif t.get('tapped_today'):
          row.update(case='ON_TRACK', expected='WORKED_ON', observed=True, resolution='EXPLAINED',
                     explanation=base + f"; {dk}: ON TRACK - floor tap today, no count recorded")
          if tier != 'Stretch':
            moved_in += load
        elif t.get('state') == 'running':
          row.update(case='STOPPED', expected='WORKED_ON', observed=False, routed_to='PC',
                     explanation=base + f"; {dk}: STOPPED - started on the tablet "
                     f"{t['first_start']:%m/%d}, last tap {t['last_tap']:%m/%d}, nothing today")
        else:
          row.update(case='UNTOUCHED', expected='WORKED_ON', routed_to='PC',
                     explanation=base + f"; {dk}: no tablet record - never started on the floor",
                     question=(f"Multi-day order {_line_txt(s)} ({dk}, share {share:,.0f}) was on "
                               f"{_fmt_day(plan_date)}'s plan and the tablet shows no Start. "
                               "Was it started? If not, what stopped it?"))
      elif worked:
        row.update(case='WORKED', expected='WORKED_ON', observed=True, resolution='EXPLAINED',
                   explanation=base + f"; work order {w['wo_status']}"
                   + (' completed since the plan day' if w['done_since'] else ' had a floor tap on the day'))
        if tier != 'Stretch':
          moved_in += load
      elif rem:
        role = _role_of(rem['actor'], rp)
        reason = (rem['note'] or '').strip()
        when = rem['event_time'].strftime('%H:%M') if rem['event_time'] else ''
        if reason:
          row.update(case='REMOVED_WITH_REASON', expected='WORKED_ON', resolution='EXPLAINED',
                     removed_by_role=role, cause_key=None,
                     explanation=base + f"; {rem['event_type']} at {when} by {rem['actor']}: {reason}")
        else:
          row.update(case='REMOVED_NO_REASON', expected='WORKED_ON', routed_to=role,
                     removed_by_role=role,
                     explanation=base + f"; {rem['event_type']} at {when} by {rem['actor']}, no reason recorded",
                     question=(f"Order {_line_txt(s)} was removed from {_fmt_day(plan_date)}'s "
                               f"plan at {when} with no reason recorded. What was the reason?"))
      elif tier == 'Stretch':
        row.update(case='UNTOUCHED_STRETCH', expected='WORKED_ON', resolution='EXPLAINED',
                   explanation=base + '; Stretch line not reached (no question)')
      elif date_moved:
        # v1.2: Sales moved the date after publish - the PC question
        # ('what stopped it?') is stale; the DATE_MOVED row below asks Sales.
        row.update(case='UNTOUCHED', expected='WORKED_ON', resolution='EXPLAINED',
                   explanation=base + f"; no activity, but ship-by moved to {new_ship_by:%m/%d} "
                   "after publish - Sales is asked instead")
      else:
        row.update(case='UNTOUCHED', expected='WORKED_ON', routed_to='PC',
                   explanation=base + '; no shipment and no work-order activity recorded',
                   question=(f"Order {_line_txt(s)} was {tier} on {_fmt_day(plan_date)} "
                             f"({load:,.0f} in" + (f", late since {s['ship_by_date']:%m-%d}" if late else '')
                             + ") and shows no shipment or work-order activity. "
                             "Was it started? If not, what stopped it?"))
      counts[row['case']] += 1
      rows.append(row)

      # date moved is a SECOND row for the same line (different 'expected')
      if date_moved and not cancelled:
        counts['DATE_MOVED'] += 1
        when = f" at {_hm(dch[-1]['detected_at'])}" if dch else ''
        rows.append({'order': key[0], 'sku': key[1], 'rel': lkey[1], 'item': lkey[2],
                     'line': _line_txt(s), 'tier': tier, 'load': load,
                     'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
                     'expected': 'DATE_HELD', 'expected_value': None, 'actual_value': None,
                     'observed': bool(dch), 'case': 'DATE_MOVED', 'routed_to': 'SALES',
                     'explanation': (f"ship-by at publish {s['ship_by_date']:%m/%d}, now "
                                     f"{new_ship_by:%m/%d}" + (f", changed{when} per the "
                                                               "work-order change log" if dch else '')),
                     'question': (f"Order {_line_txt(s)} ship-by moved from "
                                  f"{s['ship_by_date']:%m/%d} to {new_ship_by:%m/%d}{when} after "
                                  f"{_fmt_day(plan_date)}'s plan was published. Customer change, "
                                  "or an entry correction?"),
                     'asked_to': None, 'cause_key': None, 'resolution': 'OPEN',
                     'removed_by_role': None})

    # --- WHOLE-ORDER RULE (Dave, 2026-09-13): partial orders at publish ---
    # From the snapshot alone: a planned line's same-due-date siblings that
    # were NOT on the plan. One ledger row per missing sibling, case
    # PARTIAL_ORDER, routed to PC, no question (measured, not asked - the
    # Publish check already offered "Add the rest"). A PartialAck event at
    # publish means PC knew and chose to; the row says so and closes as
    # EXPLAINED. A sibling that shipped anyway is NOT an UNPLANNED_SHIP
    # (no rush question to Sales) - it is this.
    cursor.execute("""
        SELECT DISTINCT nx_order_numb FROM dbo.pc_plan_line_events
        WHERE plan_date = %s AND event_type = 'PartialAck'
    """, (plan_date,))
    acked = {r['nx_order_numb'] for r in cursor.fetchall()}
    planned_groups = {(s['nx_order_numb'], str(s['ship_by_date'] or '')[:10])
                      for s in planned}
    partial_keys = set()
    for s in unplanned_pool:
      gk = (s['nx_order_numb'], str(s['ship_by_date'] or '')[:10])
      if gk not in planned_groups:
        continue
      key = (s['nx_order_numb'], s['nx_sku'])
      lkey = _lk(s)
      partial_keys.add(lkey)
      load = float(s['load_inches'] or 0)
      b = _look(book, book_sku, s)
      shipped_anyway = bool(_chg(s, 'SO_Shipped')) or b is None
      knew = key[0] in acked
      counts['PARTIAL_ORDER'] += 1
      rows.append({'order': key[0], 'sku': key[1], 'rel': lkey[1], 'item': lkey[2],
                   'line': _line_txt(s), 'tier': None, 'load': load,
                   'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
                   'expected': 'ON_PLAN', 'expected_value': float(s['qty_open'] or 0),
                   'actual_value': (None if b is None else float(b['balance'] or 0)),
                   'observed': shipped_anyway, 'case': 'PARTIAL_ORDER', 'routed_to': 'PC',
                   'explanation': (f"whole-order rule: sibling of a planned line (order "
                                   f"{key[0]}, due {gk[1]}) was left off the plan, {load:,.0f} in"
                                   + ('; PC published anyway (acknowledged)' if knew else '')
                                   + ('; shipped anyway' if shipped_anyway else '; did not ship')),
                   'question': None, 'asked_to': None, 'cause_key': None,
                   'resolution': ('EXPLAINED' if knew else 'OPEN'),
                   'removed_by_role': None})

    # --- demand side: shipped without being planned --------------------
    unplanned_in, n_unplanned_all = 0.0, 0
    for s in unplanned_pool:
      key = (s['nx_order_numb'], s['nx_sku'])
      lkey = _lk(s)
      if lkey in partial_keys:
        continue                       # whole-order rule: that is a PARTIAL_ORDER row
      load = float(s['load_inches'] or 0)
      if load <= 0:
        continue                       # zero-touch never counts against the reserve
      qty0 = float(s['qty_open'] or 0)
      b = _look(book, book_sku, s)
      bal = float(b['balance'] or 0) if b else 0.0
      shp = _chg(s, 'SO_Shipped')
      if _chg(s, 'SO_Canceled', 'SO_Removed') and not shp:
        continue                       # v1.2: cancelled, not shipped
      if _chg(s, 'FIELD_CHANGE', field='quantity') and not shp and b is not None:
        continue                       # v1.2: quantity edited, not shipped
      shipped = bool(shp) or (b is None) or (bal < qty0 - 0.001)   # status 15 alone: no
      if not shipped:
        continue
      n_unplanned_all += 1
      unplanned_in += load
      if load >= rp['unplanned_min']:
        counts['UNPLANNED_SHIP'] += 1
        rows.append({'order': key[0], 'sku': key[1], 'rel': lkey[1], 'item': lkey[2],
                     'line': _line_txt(s), 'tier': None, 'load': load,
                     'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
                     'expected': 'ON_PLAN', 'expected_value': qty0,
                     'actual_value': (None if b is None else bal), 'observed': True,
                     'case': 'UNPLANNED_SHIP', 'routed_to': 'SALES',
                     'explanation': (f"not on {_fmt_day(plan_date)}'s plan, {load:,.0f} in, "
                                     + ('gated at publish' if s['is_gated'] else f"score {int(s['priority_score'] or 0)}")
                                     + '; shipped anyway'),
                     'question': (f"Order {_line_txt(s)} ({load:,.0f} in) shipped on "
                                  f"{_fmt_day(plan_date)}"
                                  + (f" at {_hm(shp[-1]['detected_at'])}" if shp else '')
                                  + " without being on the plan. Was this a "
                                  "customer-requested rush, and when was it requested?"),
                     'asked_to': None, 'cause_key': None, 'resolution': 'OPEN',
                     'removed_by_role': None})

    # --- ration the questions: max_q total, min_sales floor ------------
    order_key = {'STOPPED': 0, 'UNTOUCHED': 0, 'SLOW': 1, 'REMOVED_NO_REASON': 1, 'UNPLANNED_SHIP': 2,
                 'CANCELLED': 3, 'QTY_CHANGED': 4, 'DATE_MOVED': 5, 'PARTIAL_ORDER': 6}
    def _rank(r):
      # v1.2.1: planned lines rank by PLAN POSITION (the order the floor
      # saw on the Production grid), not by priority_score - one 'most
      # important' order for the day. Unplanned/demand rows keep score, load.
      return (order_key.get(r['case'], 9), 0 if r['tier'] == 'Must' else 1,
              r.get('pos', 10 ** 6), -r['score'], -r['load'])
    askable = sorted([r for r in rows if r['question']], key=_rank)
    sales_q = [r for r in askable if r['routed_to'] == 'SALES']
    pc_q = [r for r in askable if r['routed_to'] != 'SALES']
    chosen = []
    floor = min(rp['min_sales'], len(sales_q), rp['max_q'])
    chosen += sales_q[:floor]
    for r in askable:
      if len(chosen) >= rp['max_q']:
        break
      if r not in chosen:
        chosen.append(r)
    for i, r in enumerate(askable, start=1):
      r['rank'] = i
    # 096: stalled lines are ALWAYS asked (one each), on top of max_q
    stalled = _stall_scan(cursor, plan_date, rows, rp)
    stalled += _md_stall_scan(cursor, plan_date, rows, rp)      # retro-1.7: STOPPED multi-day lines
    for r in stalled:
      if not r.get('question'):
        if r in chosen:                 # a sibling carries the order's question
          chosen.remove(r)
        continue
      if r not in chosen:
        chosen.append(r)
      if r.get('rank') is None:
        r['rank'] = 0
    for r in chosen:
      r['asked'] = True
      r['asked_to'] = _owners(r['routed_to'], rp)

    # --- write the ledger (keep answered rows) -------------------------
    cursor.execute("""
        DELETE FROM dbo.pc_plan_variance
        WHERE plan_date = %s AND answer_text IS NULL AND reading_confirmed_at IS NULL
    """, (plan_date,))
    cursor.execute("""
        SELECT nx_order_numb, nx_sku, nx_rel_numb, nx_item_no, expected
        FROM dbo.pc_plan_variance WHERE plan_date = %s
    """, (plan_date,))
    kept = {(r['nx_order_numb'], r['nx_sku'], r['nx_rel_numb'] or '', r['nx_item_no'] or '',
             r['expected']) for r in cursor.fetchall()}
    n_ins = 0
    for r in rows:
      if (r['order'], r['sku'], r.get('rel') or '', r.get('item') or '', r['expected']) in kept:
        continue
      asked = bool(r.get('asked'))
      cursor.execute("""
          INSERT INTO dbo.pc_plan_variance
              (plan_date, nx_order_numb, nx_sku, nx_rel_numb, nx_item_no,
               expected, expected_value, actual_value,
               actual_observed, variance_kind, routed_to, system_explanation,
               question_text, asked_to, asked_at, cause_key, resolution, resolved_at,
               variance_case, tier, load_inches, question_rank, removed_by_role,
               evidence_class)
          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'UNCLASSIFIED', %s, %s,
                  %s, %s, CASE WHEN %s = 1 THEN GETDATE() END, %s, %s,
                  CASE WHEN %s = 1 THEN GETDATE() END,
                  %s, %s, %s, %s, %s, %s)
      """, (plan_date, r['order'], r['sku'], (r.get('rel') or None), (r.get('item') or None),
            r['expected'], r['expected_value'],
            r['actual_value'], 1 if r['observed'] else 0, r['routed_to'],
            r['explanation'][:500], (r['question'][:500] if r['question'] else None),
            (r['asked_to'] if asked else None), (1 if asked else 0),
            r['cause_key'], r['resolution'],
            (1 if r['resolution'] != 'OPEN' else 0),
            r['case'], r['tier'], round(r['load'], 2), r.get('rank'), r['removed_by_role'],
            ('Probable' if r['resolution'] == 'EXPLAINED' else None)))
      n_ins += 1

    # --- 096: the stalled lines leave the plan (holds) -----------------
    stall_holds = _place_stall_holds(cursor, plan_date, stalled, rp)

    # --- header ---------------------------------------------------------
    n_planned = len([s for s in planned if (s['tier'] or 'Next') != 'Stretch'])
    reserve_used = (round(unplanned_in / reserve_in * 100.0, 2) if reserve_in else None)
    cursor.execute("""
        MERGE dbo.pc_plan_retro AS t
        USING (SELECT %s AS plan_date) AS s ON t.plan_date = s.plan_date
        WHEN MATCHED THEN UPDATE SET run_at = SYSDATETIME(), revision_no = %s,
             n_planned = %s, n_stretch = %s, n_shipped = %s, n_worked = %s,
             n_removed_reason = %s, n_removed_noreason = %s, n_untouched = %s,
             n_date_moved = %s, n_unplanned_ship = %s, n_questions_asked = %s,
             planned_inches = %s, moved_inches = %s, unplanned_inches = %s,
             reserve_inches = %s, reserve_used_pct = %s, run_by = %s
        WHEN NOT MATCHED THEN INSERT
             (plan_date, revision_no, n_planned, n_stretch, n_shipped, n_worked,
              n_removed_reason, n_removed_noreason, n_untouched, n_date_moved,
              n_unplanned_ship, n_questions_asked, planned_inches, moved_inches,
              unplanned_inches, reserve_inches, reserve_used_pct, run_by)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """, (plan_date,
          rev, n_planned, n_stretch, counts['SHIPPED'], counts['WORKED'],
          counts['REMOVED_WITH_REASON'], counts['REMOVED_NO_REASON'], counts['UNTOUCHED'],
          counts['DATE_MOVED'], n_unplanned_all, len(chosen),
          round(planned_in, 2), round(moved_in, 2), round(unplanned_in, 2),
          reserve_in, reserve_used, actor,
          plan_date, rev, n_planned, n_stretch, counts['SHIPPED'], counts['WORKED'],
          counts['REMOVED_WITH_REASON'], counts['REMOVED_NO_REASON'], counts['UNTOUCHED'],
          counts['DATE_MOVED'], n_unplanned_all, len(chosen),
          round(planned_in, 2), round(moved_in, 2), round(unplanned_in, 2),
          reserve_in, reserve_used, actor))
    # --- 067: the day's ACTUAL crew and capacity (pro-rated attendance) ---
    # Same formula as vw_pc_capacity_day, on the plan date's credited
    # attendance (vw_em_assembler_credit): day override > standing factor x
    # credit > credit. Stored beside the stamped (morning) capacity so the
    # calibration can compare shipped inches with hours actually worked.
    crew_actual = cap_actual = None
    try:
      cursor.execute("""
          SELECT SUM(COALESCE(o.capacity_factor,
                              r.default_capacity_factor * ISNULL(c.credit, 0),
                              ISNULL(c.credit, 0))) AS crew
          FROM dbo.em_assembler_roster r
          LEFT JOIN dbo.vw_em_assembler_credit c
                 ON c.employee_id = r.employee_id AND c.status_date = %s
          LEFT JOIN dbo.pc_assembler_day_override o
                 ON o.employee_id = r.employee_id AND o.override_date = %s
          WHERE r.end_date IS NULL
            AND (r.start_date IS NULL OR r.start_date <= %s)
      """, (plan_date, plan_date, plan_date))
      crew_actual = float((cursor.fetchone() or {}).get('crew') or 0)
      cursor.execute("""
          SELECT MAX(CASE WHEN pw.factor_code = 'ST_STD' THEN pw.factor_value END) AS st_std,
                 MAX(CASE WHEN pw.factor_code = 'BASE_OVERHEAD' THEN pw.factor_value END) AS base_hrs
          FROM dbo.pc_weight_set ws JOIN dbo.pc_weight pw ON pw.weight_set_id = ws.weight_set_id
          WHERE ws.retired_date IS NULL
      """)
      w = cursor.fetchone() or {}
      p = _get_params(cursor)
      hours = float(p.get('HOURS_PER_DAY', 8.0)); eff = float(p.get('EFFICIENCY_FACTOR', 1.2)) or 1.0
      st_std = float(w.get('st_std') or 0); base = float(w.get('base_hrs') or 0)
      if st_std:
        cap_actual = round(crew_actual * (hours * 60.0 - base * 60.0 / 5.0) / eff / st_std, 1)
      cursor.execute("""
          UPDATE dbo.pc_plan_retro SET crew_actual = %s, capacity_actual_inches = %s
          WHERE plan_date = %s
      """, (round(crew_actual, 2), cap_actual, plan_date))
    except Exception:
      pass                          # 067 not applied yet, or no attendance: the retro still stands
    conn.commit()

    return _jsonable({
      'status': 'success', 'plan_date': plan_date.isoformat(), 'revision_no': rev,
      'version': _RETRO_VERSION, 'capacity_inches': cap_in,
      'crew_actual': crew_actual, 'capacity_actual_inches': cap_actual,
      'n_planned': n_planned, 'n_stretch': n_stretch, 'counts': counts,
      'n_unplanned_all': n_unplanned_all,
      'planned_inches': round(planned_in, 1), 'moved_inches': round(moved_in, 1),
      'unplanned_inches': round(unplanned_in, 1), 'reserve_inches': reserve_in,
      'reserve_used_pct': reserve_used,
      'n_questions': len(chosen), 'n_askable': len(askable), 'rows_written': n_ins,
      'n_stalled': len(stall_holds), 'stalled': stall_holds,          # 096
      'questions': [{'order': r['order'], 'sku': r['sku'], 'item': r.get('item'),
                     'line': r.get('line'), 'case': r['case'],
                     'to': r['routed_to'], 'owners': r['asked_to'], 'rank': r['rank'],
                     'text': r['question']} for r in chosen],
      'not_asked': [{'order': r['order'], 'sku': r['sku'], 'item': r.get('item'),
                     'line': r.get('line'), 'case': r['case'],
                     'to': r['routed_to'], 'rank': r['rank'], 'text': r['question']}
                    for r in askable if r not in chosen],
      'lines': [{'order': r['order'], 'sku': r['sku'], 'item': r.get('item'),
                 'line': r.get('line'), 'tier': r['tier'], 'load': r['load'],
                 'case': r['case'], 'explanation': r['explanation']}
                for r in rows if r['expected'] != 'DATE_HELD']})
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Retro run failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
def retro_run(plan_date=None, force=False):
  return _retro_run_impl(plan_date, force)


@anvil.server.http_endpoint('/agent/retro', methods=['POST'])
def agent_retro_endpoint(**kwargs):
  """titan-ai-service calls this at 06:30 (before the proposal). Runs
  synchronously - one day, a handful of queries."""
  if not _agent_key_ok():
    return anvil.server.HttpResponse(401, 'unauthorized')
  body = anvil.server.request.body_json or {}
  return _retro_run_impl(body.get('plan_date'), bool(body.get('force')), actor='agent')


# =============================================================================
# THE RETRO FORM
# =============================================================================

@anvil.server.callable
def retro_get_day(plan_date=None):
  """Everything the Retro form shows for one day, plus Dave's queue across
  all days and the cause list."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    rp = _retro_params(cursor)
    if plan_date:
      plan_date = _parse_date(plan_date)
    else:
      cursor.execute("SELECT MAX(plan_date) AS d FROM dbo.pc_plan_retro")
      plan_date = (cursor.fetchone() or {}).get('d') or _prev_business_day()
    cursor.execute("SELECT * FROM dbo.pc_plan_retro WHERE plan_date = %s", (plan_date,))
    hdr = cursor.fetchone()
    cursor.execute("""
        SELECT v.*, k.label AS proposed_cause_label
        FROM dbo.pc_plan_variance v
        LEFT JOIN dbo.pc_plan_cause_keys k ON k.cause_key = v.proposed_cause_key
        WHERE v.plan_date = %s
        ORDER BY CASE WHEN v.asked_at IS NOT NULL THEN 0
                      WHEN v.question_text IS NOT NULL THEN 1 ELSE 2 END,
                 v.question_rank, v.load_inches DESC
    """, (plan_date,))
    rows = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT v.variance_id, v.plan_date, v.nx_order_numb, v.nx_sku, v.nx_rel_numb,
               v.nx_item_no, v.variance_case,
               v.routed_to, v.question_text, v.answer_text, v.answered_by, v.answered_at,
               v.proposed_reading, v.proposed_evidence_class, v.proposed_cause_key
        FROM dbo.pc_plan_variance v
        WHERE v.answer_text IS NOT NULL AND v.reading_confirmed_at IS NULL
          AND v.resolution IN ('OPEN','DISPUTED')
        ORDER BY v.answered_at
    """)
    queue = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT cause_key, label, side, variance_kind FROM dbo.pc_plan_cause_keys
        WHERE is_active = 1 ORDER BY sort_order
    """)
    causes = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT cause_key, COUNT(*) AS n
        FROM dbo.pc_plan_variance
        WHERE cause_key IS NOT NULL AND reading_confirmed_at IS NOT NULL
          AND evidence_class IN ('Verified','Probable')
          AND plan_date >= DATEADD(day, -14, %s)
        GROUP BY cause_key ORDER BY n DESC
    """, (plan_date,))
    week = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("SELECT MIN(plan_date) AS lo, MAX(plan_date) AS hi FROM dbo.pc_plan_retro")
    rng = cursor.fetchone() or {}
    cursor.execute("""
        SELECT TOP 15 r.plan_date,
               (SELECT COUNT(*) FROM dbo.pc_plan_variance v
                WHERE v.plan_date = r.plan_date AND v.asked_at IS NOT NULL
                  AND v.answer_text IS NULL AND v.resolution = 'OPEN') AS n_open
        FROM dbo.pc_plan_retro r ORDER BY r.plan_date DESC
    """)
    days = [{'plan_date': make_serializable(d['plan_date']), 'n_open': int(d['n_open'] or 0)}
            for d in cursor.fetchall()]
    n_open = {'PC': 0, 'SALES': 0}
    for v in rows:
      if v.get('asked_at') and not v.get('answer_text') and v.get('resolution') == 'OPEN':
        n_open['SALES' if v.get('routed_to') == 'SALES' else 'PC'] += 1
    return {'status': 'success', 'plan_date': plan_date.isoformat(),
            'header': serialize_row(hdr) if hdr else None,
            'variances': rows, 'dave_queue': queue, 'causes': causes,
            'week_causes': week, 'params': {k: v for k, v in rp.items()},
            'answerers': {'PC': rp['pc_owners'], 'SALES': rp['sales_owners']},
            'days': days, 'n_open': n_open,
            'first_day': make_serializable(rng.get('lo')),
            'last_day': make_serializable(rng.get('hi'))}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def retro_get_review():
  """Dave's #retrev form: every answered-but-unconfirmed question across
  all days (the queue), the last confirmed ones, the cause list, the
  14-day cause counts, and which days still owe answers."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT v.variance_id, v.plan_date, v.nx_order_numb, v.nx_sku, v.nx_rel_numb,
               v.nx_item_no, v.variance_case,
               v.routed_to, v.tier, v.load_inches, v.system_explanation, v.question_text,
               v.asked_to, v.answer_text, v.answered_by, v.answered_at,
               v.proposed_reading, v.proposed_evidence_class, v.proposed_cause_key,
               k.label AS proposed_cause_label, v.resolution
        FROM dbo.pc_plan_variance v
        LEFT JOIN dbo.pc_plan_cause_keys k ON k.cause_key = v.proposed_cause_key
        WHERE v.answer_text IS NOT NULL AND v.reading_confirmed_at IS NULL
        ORDER BY v.answered_at
    """)
    queue = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT TOP 20 v.variance_id, v.plan_date, v.nx_order_numb, v.nx_sku, v.nx_rel_numb,
               v.nx_item_no, v.variance_case,
               v.routed_to, v.answer_text, v.answered_by, v.confirmed_reading,
               v.evidence_class, v.cause_key, v.resolution, v.reading_confirmed_at
        FROM dbo.pc_plan_variance v
        WHERE v.reading_confirmed_at IS NOT NULL
        ORDER BY v.reading_confirmed_at DESC
    """)
    recent = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT plan_date,
               SUM(CASE WHEN asked_at IS NOT NULL THEN 1 ELSE 0 END) AS n_asked,
               SUM(CASE WHEN asked_at IS NOT NULL AND answer_text IS NULL
                         AND resolution = 'OPEN' THEN 1 ELSE 0 END) AS n_unanswered,
               SUM(CASE WHEN answer_text IS NOT NULL AND reading_confirmed_at IS NULL
                        THEN 1 ELSE 0 END) AS n_awaiting_dave
        FROM dbo.pc_plan_variance
        WHERE plan_date >= DATEADD(day, -21, CAST(GETDATE() AS DATE))
        GROUP BY plan_date ORDER BY plan_date DESC
    """)
    days = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT cause_key, label, side, variance_kind FROM dbo.pc_plan_cause_keys
        WHERE is_active = 1 ORDER BY sort_order
    """)
    causes = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT v.cause_key, k.label, COUNT(*) AS n
        FROM dbo.pc_plan_variance v
        LEFT JOIN dbo.pc_plan_cause_keys k ON k.cause_key = v.cause_key
        WHERE v.cause_key IS NOT NULL AND v.reading_confirmed_at IS NOT NULL
          AND v.evidence_class IN ('Verified','Probable')
          AND v.plan_date >= DATEADD(day, -14, CAST(GETDATE() AS DATE))
        GROUP BY v.cause_key, k.label ORDER BY n DESC
    """)
    week = [serialize_row(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT rule_id, title, status, cause_key, recurrence_count, proposed_at
        FROM dbo.pc_plan_rule_register
        WHERE status IN ('PROPOSED','NEEDS_BUILD') ORDER BY proposed_at DESC
    """)
    rules = [serialize_row(r) for r in cursor.fetchall()]
    return {'status': 'success', 'queue': queue, 'recent': recent, 'days': days,
            'causes': causes, 'week_causes': week, 'rule_candidates': rules}
  except Exception as e:
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def retro_answer(variance_id, answer_text, answered_by):
  """Free text, verbatim. Anyone can answer; the form asks who."""
  answer_text = (answer_text or '').strip()
  answered_by = (answered_by or '').strip() or _current_actor()
  if not answer_text:
    return {'status': 'error', 'message': 'Answer is empty.'}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dbo.pc_plan_variance
        SET answer_text = %s, answered_by = %s, answered_at = GETDATE(),
            proposed_reading = NULL, proposed_evidence_class = NULL,
            proposed_cause_key = NULL
        WHERE variance_id = %s AND reading_confirmed_at IS NULL
    """, (answer_text[:2000], answered_by[:50], int(variance_id)))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'Question not found, or already confirmed by Dave.'}
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


def _arbiter_ok(pin):
  """Dave confirms readings - always Dave. The app has no login yet, so
  the confirm step takes a PIN held in the Anvil Secret RETRO_ARBITER_PIN.
  No secret configured = open (first days), and the response says so."""
  try:
    expected = anvil.secrets.get_secret('RETRO_ARBITER_PIN')
  except Exception:
    expected = None
  if not expected:
    return True, 'no RETRO_ARBITER_PIN secret - confirm is unguarded'
  return (str(pin or '').strip() == str(expected).strip()), None


@anvil.server.callable
def retro_confirm(variance_id, reading, evidence_class, cause_key,
                  resolution='EXPLAINED', pin=None, note=None):
  ok, warn = _arbiter_ok(pin)
  if not ok:
    return {'status': 'error', 'message': 'Wrong PIN - only Dave confirms readings.'}
  if evidence_class not in ('Verified', 'Probable', 'Unverified', 'Disputed', 'Unknown'):
    return {'status': 'error', 'message': f'Bad evidence class {evidence_class}.'}
  if resolution not in ('EXPLAINED', 'EXCEPTION', 'RULE_CANDIDATE', 'CLOSED_UNKNOWN', 'DISPUTED'):
    return {'status': 'error', 'message': f'Bad resolution {resolution}.'}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("SELECT variance_kind FROM dbo.pc_plan_cause_keys WHERE cause_key = %s",
                   (cause_key,))
    ck = cursor.fetchone()
    if not ck:
      return {'status': 'error', 'message': f'Unknown cause key {cause_key}.'}
    cursor.execute("""
        UPDATE dbo.pc_plan_variance
        SET confirmed_reading = %s, reading_confirmed_at = GETDATE(),
            evidence_class = %s, evidence_note = %s, cause_key = %s,
            variance_kind = %s, resolution = %s, resolved_at = GETDATE()
        WHERE variance_id = %s AND answer_text IS NOT NULL
    """, ((reading or '')[:500], evidence_class, (note or None), cause_key,
          ck['variance_kind'], resolution, int(variance_id)))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'Nothing to confirm (no answer yet?).'}
    # 071: a CONFIRMED material_short reading places a material hold on the
    # e21 line (X05 gate) - Dave is the gatekeeper, so it happens here and
    # nowhere earlier. Silent no-op until migration 071 is applied.
    # 2026-09-24 (rule register: component_wait x5): the cause keys that
    # place a hold are a parameter, HOLD_CAUSE_KEYS (comma-separated;
    # default = material_short only), so approving the candidate is a
    # settings change, not a code change.
    hold = None
    if cause_key in _hold_cause_keys(cursor) and resolution in ('EXPLAINED', 'EXCEPTION', 'RULE_CANDIDATE'):
      hold = _place_material_hold(cursor, int(variance_id), reading)
    else:
      # 100 (decision 3): the agent may already have placed a hold from the
      # PC answer (placed_by 'agent (PC answer)'). Dave reading it as
      # anything else is the undo - release it, note why.
      released = _release_agent_hold(cursor, int(variance_id), cause_key, resolution)
      if released:
        hold = {'released': True, 'line': released}
    conn.commit()
    return {'status': 'success', 'warning': warn, 'hold': hold}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


def _release_agent_hold(cursor, variance_id, cause_key, resolution):
  """100: release a live MATERIAL hold the agent placed from this variance's
  answer when Dave's reading is not a hold cause. Returns the line label
  or None. Holds Dave placed himself (071) are never touched here."""
  cursor.execute("""
      SELECT TOP 1 h.nx_order_numb, h.nx_rel_numb, h.nx_item_no, h.nx_sku
      FROM dbo.pc_plan_line_hold h
      WHERE h.source_variance_id = %s AND h.hold_kind = 'MATERIAL'
        AND h.placed_by = 'agent (PC answer)' AND h.released_at IS NULL
  """, (variance_id,))
  h = cursor.fetchone()
  if not h:
    return None
  cursor.execute("""
      EXEC dbo.sp_pc_release_line_hold @order = %s, @rel = %s, @item = %s,
           @released_by = %s, @note = %s
  """, (h['nx_order_numb'], h['nx_rel_numb'], h['nx_item_no'], 'Dave (review)',
        f"read as {cause_key} / {resolution} - agent hold undone"[:200]))
  cursor.fetchone()
  return _line_txt({'nx_order_numb': h['nx_order_numb'], 'nx_rel_numb': h['nx_rel_numb'],
                    'nx_item_no': h['nx_item_no'], 'nx_sku': h['nx_sku']})


def _hold_cause_keys(cursor):
  """Cause keys whose confirmed reading places an X05 hold (HOLD_CAUSE_KEYS,
  pc_plan_parameters). Missing or blank = material_short, the 071 behaviour."""
  cursor.execute("SELECT param_value FROM dbo.pc_plan_parameters WHERE param_code = 'HOLD_CAUSE_KEYS'")
  r = cursor.fetchone()
  raw = (r['param_value'] if r else None) or 'material_short'
  return {k.strip() for k in raw.split(',') if k.strip()}


def _place_material_hold(cursor, variance_id, reading):
  """071: EXEC dbo.sp_pc_place_line_hold for the variance's e21 line.
  The proc inserts/extends the hold, unplaces live plan lines for the item
  (today onward), logs the events and re-stamps published days. Returns a
  small dict for the review form's notification, or None."""
  cursor.execute("SELECT OBJECT_ID('dbo.sp_pc_place_line_hold') AS o")
  if not (cursor.fetchone() or {}).get('o'):
    return None
  cursor.execute("""
      SELECT v.plan_date, v.nx_order_numb, v.nx_sku, v.nx_rel_numb, v.nx_item_no
      FROM dbo.pc_plan_variance v WHERE v.variance_id = %s
  """, (variance_id,))
  v = cursor.fetchone()
  if not v:
    return None
  rel, item = v.get('nx_rel_numb'), v.get('nx_item_no')
  if not (rel and item):
    # pre-066 variance row: resolve the item through the plan line
    cursor.execute("""
        SELECT TOP 1 nx_rel_numb, nx_item_no FROM dbo.pc_ship_plan_line
        WHERE plan_date = %s AND nx_order_numb = %s AND nx_sku = %s
        ORDER BY nx_rel_numb, nx_item_no
    """, (v['plan_date'], v['nx_order_numb'], v['nx_sku']))
    pl = cursor.fetchone()
    if not pl:
      return None
    rel, item = pl['nx_rel_numb'], pl['nx_item_no']
  reason = (reading or '').strip() or 'Material short (retro answer)'
  cursor.execute("""
      EXEC dbo.sp_pc_place_line_hold @order = %s, @rel = %s, @item = %s,
           @reason = %s, @placed_by = %s, @source_variance_id = %s
  """, (v['nx_order_numb'], rel, item, reason[:500], 'Dave (retro)', variance_id))
  r = cursor.fetchone() or {}
  return {'line': _line_txt({'nx_order_numb': v['nx_order_numb'], 'nx_rel_numb': rel,
                             'nx_item_no': item, 'nx_sku': v.get('nx_sku')}),
          'expires_on': make_serializable(r.get('expires_on')),
          'n_unplaced': int(r.get('n_unplaced') or 0),
          'created': bool(r.get('created'))}


# ── the rules register: Dave's decision on a candidate (2026-09-24) ─────
# The Monday scan proposes rows; until today nothing in the app could decide
# one. rule_list feeds a "Rule candidates" section on #retrev; rule_decide
# flips the status (PIN-gated like retro_confirm). Approving records the
# decision - the EFFECT is a parameter or a flag change, named in the row.

_RULE_DECISIONS = ('APPROVED', 'REJECTED', 'NEEDS_BUILD', 'PROPOSED')


@anvil.server.callable
def rule_list(status=None, limit=50):
  """Register rows, newest first. status None = the open ones
  (PROPOSED / NEEDS_BUILD); 'ALL' = everything."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    where = ("WHERE status IN ('PROPOSED','NEEDS_BUILD')" if not status
             else "" if str(status).upper() == 'ALL'
             else "WHERE status = %s")
    args = () if not status or str(status).upper() == 'ALL' else (str(status).upper(),)
    cursor.execute(f"""
        SELECT TOP ({int(limit)}) rule_id, rule_kind, status, title, description, scope_type,
               scope_value, effect_flag_code, score_adjustment, gate_override_code,
               new_weight, new_threshold, effective_from, expires_on, cause_key,
               recurrence_count, evidence_variance_ids, test_summary, proposed_by,
               proposed_at, approved_by, approved_at, decision_note
        FROM dbo.pc_plan_rule_register {where}
        ORDER BY CASE status WHEN 'PROPOSED' THEN 0 WHEN 'NEEDS_BUILD' THEN 1 ELSE 2 END,
                 proposed_at DESC
    """, args)
    return [serialize_row(r) for r in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
def rule_decide(rule_id, status, note=None, pin=None):
  """Set a register row's status with a note. Only Dave decides (PIN).
  APPROVED stamps approved_by/at; REJECTED keeps the row as the record of
  why not. The effect itself (parameter, flag) is applied separately and
  should be named in the note."""
  ok, warn = _arbiter_ok(pin)
  if not ok:
    return {'status': 'error', 'message': 'Wrong PIN - only Dave decides rules.'}
  status = str(status or '').strip().upper()
  if status not in _RULE_DECISIONS:
    return {'status': 'error', 'message': f'Bad status {status}.'}
  actor = _current_actor() or 'Dave'
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dbo.pc_plan_rule_register
        SET status = %s,
            decision_note = %s,
            approved_by = CASE WHEN %s = 'APPROVED' THEN %s ELSE approved_by END,
            approved_at = CASE WHEN %s = 'APPROVED' THEN SYSDATETIME() ELSE approved_at END
        WHERE rule_id = %s
    """, (status, (note or None), status, actor[:50], status, int(rule_id)))
    if cursor.rowcount == 0:
      conn.rollback()
      return {'status': 'error', 'message': f'Rule {rule_id} not found.'}
    conn.commit()
    return {'status': 'success', 'rule_id': int(rule_id), 'new_status': status, 'warning': warn}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()


@anvil.server.callable
def retro_ask_anyway(variance_id):
  """Promote a 'not asked today' variance to an asked question."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    rp = _retro_params(cursor)
    cursor.execute("SELECT routed_to FROM dbo.pc_plan_variance WHERE variance_id = %s",
                   (int(variance_id),))
    r = cursor.fetchone()
    if not r:
      return {'status': 'error', 'message': 'Not found.'}
    cursor.execute("""
        UPDATE dbo.pc_plan_variance
        SET asked_at = GETDATE(), asked_to = %s
        WHERE variance_id = %s AND question_text IS NOT NULL AND asked_at IS NULL
    """, (_owners(r['routed_to'], rp), int(variance_id)))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': 'Already asked, or not a question.'}
    cursor.execute("""
        UPDATE dbo.pc_plan_retro SET n_questions_asked = n_questions_asked + 1
        WHERE plan_date = (SELECT plan_date FROM dbo.pc_plan_variance WHERE variance_id = %s)
    """, (int(variance_id),))
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'{e}'}
  finally:
    conn.close()