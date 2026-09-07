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
    SHIPPED   line gone from the open book, item status 15, or balance
              below the published qty (sales_orders_due, hourly)
    WORKED    a work order for the line completed since the plan day, or
              a WO Assign/Start/Complete event on the plan day
    REMOVED   plan event Removed / TierChange after publish - with a reason
              it is a variance with the cause attached (counts toward the
              N=3 recurrence); without one it earns a question
    UNTOUCHED none of the above (Must/Next earn a PC question; Stretch is
              optional by definition and closes silently)
    DATE_MOVED    ship-by on the open line != ship-by at publish   -> Sales
    UNPLANNED_SHIP shipped on the day, not planned, load >= threshold -> Sales
Every inferred outcome enters the ledger as evidence 'Probable' at best;
'Verified' needs a person (Dave confirms the reading - always Dave).

Callables (the Retro form):
    retro_get_day(plan_date)                 header + all variances + cause list
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
from datetime import datetime, date, timedelta

from .daily_ship_plan import (get_connection, _parse_date, _current_actor,
_get_params, _agent_key_ok, serialize_row,
make_serializable)

_RETRO_VERSION = 'retro-1.0'
_WO_DONE = ('Complete', 'Shipped - Verify', 'Closed - Verify')


# =============================================================================
# HELPERS
# =============================================================================

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
    cap_in = float(hdr.get('capacity_inches') or 0)
    reserve_in = round(cap_in * (1.0 - rp['fill_pct']), 2) if cap_in else None

    cursor.execute("""
        SELECT nx_order_numb, nx_sku, was_planned, tier, is_gated, priority_score,
               flag_codes, ship_by_date, qty_open, load_inches
        FROM dbo.pc_plan_flag_snapshot
        WHERE plan_date = %s AND revision_no = %s
    """, (plan_date, rev))
    snap = cursor.fetchall()
    planned = [s for s in snap if s['was_planned']]
    unplanned_pool = [s for s in snap if not s['was_planned']]

    # --- what the records say now -------------------------------------
    cursor.execute("""
        SELECT nx_order_numb, nx_sku,
               SUM(nx_qty_balance_due) AS balance,
               MAX(nx_item_status)     AS item_status,
               MIN(CAST(COALESCE(nx_dship_by_date, nx_hship_by_date) AS DATE)) AS ship_by
        FROM dbo.sales_orders_due
        GROUP BY nx_order_numb, nx_sku
    """)
    book = {(r['nx_order_numb'], r['nx_sku']): r for r in cursor.fetchall()}

    cursor.execute("""
        SELECT wo.sales_order_number AS o, wo.part_number AS s,
               MAX(CASE WHEN wo.status IN ('Complete','Shipped - Verify','Closed - Verify')
                         AND wo.completed_at >= %s THEN 1 ELSE 0 END) AS done_since,
               MAX(CASE WHEN e.event_id IS NOT NULL THEN 1 ELSE 0 END)   AS touched,
               MAX(wo.status) AS wo_status
        FROM dbo.work_orders wo
        LEFT JOIN dbo.work_order_events e
               ON e.work_order_id = wo.work_order_id
              AND e.event_type IN ('Assign','Start','Complete','Restart')
              AND CAST(e.event_at AS DATE) = %s
        WHERE wo.status <> 'Cancelled'
        GROUP BY wo.sales_order_number, wo.part_number
    """, (plan_date, plan_date))
    wos = {(r['o'], r['s']): r for r in cursor.fetchall()}

    cursor.execute("""
        SELECT nx_order_numb, nx_sku, event_type, actor, note, new_value, event_time
        FROM dbo.pc_plan_line_events
        WHERE plan_date = %s AND event_type IN ('Removed','TierChange','OrderRemoved')
          AND nx_order_numb IS NOT NULL
        ORDER BY event_time
    """, (plan_date,))
    removals = {}
    published_at = hdr.get('published_at')
    for e in cursor.fetchall():
      if published_at and e['event_time'] and e['event_time'] < published_at:
        continue                       # pre-publish edits are the plan, not a variance
      removals[(e['nx_order_numb'], e['nx_sku'])] = e

    # --- classify every planned line ----------------------------------
    rows, counts = [], {'SHIPPED': 0, 'WORKED': 0, 'REMOVED_WITH_REASON': 0,
                        'REMOVED_NO_REASON': 0, 'UNTOUCHED': 0,
                        'UNTOUCHED_STRETCH': 0, 'DATE_MOVED': 0, 'UNPLANNED_SHIP': 0}
    planned_in = moved_in = 0.0
    n_stretch = 0
    for s in planned:
      key = (s['nx_order_numb'], s['nx_sku'])
      tier = s['tier'] or 'Next'
      load = float(s['load_inches'] or 0)
      qty0 = float(s['qty_open'] or 0)
      if tier == 'Stretch':
        n_stretch += 1
      else:
        planned_in += load
      b = book.get(key)
      w = wos.get(key)
      bal = float(b['balance'] or 0) if b else 0.0
      shipped = (b is None) or (b['item_status'] == '15') or (bal < qty0 - 0.001)
      worked = bool(w and (w['done_since'] or w['touched']))
      rem = removals.get(key)
      late = (s['ship_by_date'] and s['ship_by_date'] < plan_date)
      base = (f"{tier} on {_fmt_day(plan_date)}, {load:,.0f} in"
              + (f", late since {s['ship_by_date']:%m-%d}" if late else '')
              + (f", flags {s['flag_codes']}" if s.get('flag_codes') else ''))
      row = {'order': key[0], 'sku': key[1], 'tier': tier, 'load': load,
             'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
             'expected': 'SHIPPED', 'expected_value': qty0,
             'actual_value': (None if b is None else bal),
             'observed': False, 'case': None, 'routed_to': None,
             'explanation': base, 'question': None, 'asked_to': None,
             'cause_key': None, 'resolution': 'OPEN', 'removed_by_role': None}
      if shipped:
        row.update(case='SHIPPED', observed=True, resolution='EXPLAINED',
                   explanation=base + ('; line left the open book' if b is None else
                                       f"; balance {qty0:,.0f} -> {bal:,.0f}"
                                       + (' (status 15)' if b['item_status'] == '15' else '')))
        if tier != 'Stretch':
          moved_in += load
      elif worked:
        row.update(case='WORKED', expected='WORKED_ON', observed=True, resolution='EXPLAINED',
                   explanation=base + f"; work order {w['wo_status']}"
                   + (' completed since the plan day' if w['done_since'] else ' had floor activity on the day'))
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
                     question=(f"Order {key[0]} / {key[1]} was removed from {_fmt_day(plan_date)}'s "
                               f"plan at {when} with no reason recorded. What was the reason?"))
      elif tier == 'Stretch':
        row.update(case='UNTOUCHED_STRETCH', expected='WORKED_ON', resolution='EXPLAINED',
                   explanation=base + '; Stretch line not reached (no question)')
      else:
        row.update(case='UNTOUCHED', expected='WORKED_ON', routed_to='PC',
                   explanation=base + '; no shipment and no work-order activity recorded',
                   question=(f"Order {key[0]} / {key[1]} was {tier} on {_fmt_day(plan_date)} "
                             f"({load:,.0f} in" + (f", late since {s['ship_by_date']:%m-%d}" if late else '')
                             + ") and shows no shipment or work-order activity. "
                             "Was it started? If not, what stopped it?"))
      counts[row['case']] += 1
      rows.append(row)

      # date moved is a SECOND row for the same line (different 'expected')
      if b is not None and s['ship_by_date'] and b['ship_by'] and b['ship_by'] != s['ship_by_date']:
        counts['DATE_MOVED'] += 1
        rows.append({'order': key[0], 'sku': key[1], 'tier': tier, 'load': load,
                     'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
                     'expected': 'DATE_HELD', 'expected_value': None, 'actual_value': None,
                     'observed': False, 'case': 'DATE_MOVED', 'routed_to': 'SALES',
                     'explanation': f"ship-by at publish {s['ship_by_date']:%m/%d}, now {b['ship_by']:%m/%d}",
                     'question': (f"Order {key[0]} / {key[1]} ship-by moved from "
                                  f"{s['ship_by_date']:%m/%d} to {b['ship_by']:%m/%d} after "
                                  f"{_fmt_day(plan_date)}'s plan was published. Customer change, "
                                  "or an entry correction?"),
                     'asked_to': None, 'cause_key': None, 'resolution': 'OPEN',
                     'removed_by_role': None})

    # --- demand side: shipped without being planned --------------------
    unplanned_in, n_unplanned_all = 0.0, 0
    for s in unplanned_pool:
      key = (s['nx_order_numb'], s['nx_sku'])
      load = float(s['load_inches'] or 0)
      if load <= 0:
        continue                       # zero-touch never counts against the reserve
      qty0 = float(s['qty_open'] or 0)
      b = book.get(key)
      bal = float(b['balance'] or 0) if b else 0.0
      shipped = (b is None) or (b['item_status'] == '15') or (bal < qty0 - 0.001)
      if not shipped:
        continue
      n_unplanned_all += 1
      unplanned_in += load
      if load >= rp['unplanned_min']:
        counts['UNPLANNED_SHIP'] += 1
        rows.append({'order': key[0], 'sku': key[1], 'tier': None, 'load': load,
                     'score': int(s['priority_score'] or 0), 'ship_by': s['ship_by_date'],
                     'expected': 'ON_PLAN', 'expected_value': qty0,
                     'actual_value': (None if b is None else bal), 'observed': True,
                     'case': 'UNPLANNED_SHIP', 'routed_to': 'SALES',
                     'explanation': (f"not on {_fmt_day(plan_date)}'s plan, {load:,.0f} in, "
                                     + ('gated at publish' if s['is_gated'] else f"score {int(s['priority_score'] or 0)}")
                                     + '; shipped anyway'),
                     'question': (f"Order {key[0]} / {key[1]} ({load:,.0f} in) shipped on "
                                  f"{_fmt_day(plan_date)} without being on the plan. Was this a "
                                  "customer-requested rush, and when was it requested?"),
                     'asked_to': None, 'cause_key': None, 'resolution': 'OPEN',
                     'removed_by_role': None})

    # --- ration the questions: max_q total, min_sales floor ------------
    order_key = {'UNTOUCHED': 0, 'REMOVED_NO_REASON': 1, 'UNPLANNED_SHIP': 2, 'DATE_MOVED': 3}
    def _rank(r):
      return (order_key.get(r['case'], 9), 0 if r['tier'] == 'Must' else 1,
              -r['score'], -r['load'])
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
    for r in chosen:
      r['asked'] = True
      r['asked_to'] = _owners(r['routed_to'], rp)

    # --- write the ledger (keep answered rows) -------------------------
    cursor.execute("""
        DELETE FROM dbo.pc_plan_variance
        WHERE plan_date = %s AND answer_text IS NULL AND reading_confirmed_at IS NULL
    """, (plan_date,))
    cursor.execute("""
        SELECT nx_order_numb, nx_sku, expected FROM dbo.pc_plan_variance WHERE plan_date = %s
    """, (plan_date,))
    kept = {(r['nx_order_numb'], r['nx_sku'], r['expected']) for r in cursor.fetchall()}
    n_ins = 0
    for r in rows:
      if (r['order'], r['sku'], r['expected']) in kept:
        continue
      asked = bool(r.get('asked'))
      cursor.execute("""
          INSERT INTO dbo.pc_plan_variance
              (plan_date, nx_order_numb, nx_sku, expected, expected_value, actual_value,
               actual_observed, variance_kind, routed_to, system_explanation,
               question_text, asked_to, asked_at, cause_key, resolution, resolved_at,
               variance_case, tier, load_inches, question_rank, removed_by_role,
               evidence_class)
          VALUES (%s, %s, %s, %s, %s, %s, %s, 'UNCLASSIFIED', %s, %s,
                  %s, %s, CASE WHEN %s = 1 THEN GETDATE() END, %s, %s,
                  CASE WHEN %s = 1 THEN GETDATE() END,
                  %s, %s, %s, %s, %s, %s)
      """, (plan_date, r['order'], r['sku'], r['expected'], r['expected_value'],
            r['actual_value'], 1 if r['observed'] else 0, r['routed_to'],
            r['explanation'][:500], (r['question'][:500] if r['question'] else None),
            (r['asked_to'] if asked else None), (1 if asked else 0),
            r['cause_key'], r['resolution'],
            (1 if r['resolution'] != 'OPEN' else 0),
            r['case'], r['tier'], round(r['load'], 2), r.get('rank'), r['removed_by_role'],
            ('Probable' if r['resolution'] == 'EXPLAINED' else None)))
      n_ins += 1

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
    conn.commit()

    return _jsonable({
      'status': 'success', 'plan_date': plan_date.isoformat(), 'revision_no': rev,
      'version': _RETRO_VERSION, 'capacity_inches': cap_in,
      'n_planned': n_planned, 'n_stretch': n_stretch, 'counts': counts,
      'n_unplanned_all': n_unplanned_all,
      'planned_inches': round(planned_in, 1), 'moved_inches': round(moved_in, 1),
      'unplanned_inches': round(unplanned_in, 1), 'reserve_inches': reserve_in,
      'reserve_used_pct': reserve_used,
      'n_questions': len(chosen), 'n_askable': len(askable), 'rows_written': n_ins,
      'questions': [{'order': r['order'], 'sku': r['sku'], 'case': r['case'],
                     'to': r['routed_to'], 'owners': r['asked_to'], 'rank': r['rank'],
                     'text': r['question']} for r in chosen],
      'not_asked': [{'order': r['order'], 'sku': r['sku'], 'case': r['case'],
                     'to': r['routed_to'], 'rank': r['rank'], 'text': r['question']}
                    for r in askable if r not in chosen],
      'lines': [{'order': r['order'], 'sku': r['sku'], 'tier': r['tier'], 'load': r['load'],
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
        SELECT v.variance_id, v.plan_date, v.nx_order_numb, v.nx_sku, v.variance_case,
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
    return {'status': 'success', 'plan_date': plan_date.isoformat(),
            'header': serialize_row(hdr) if hdr else None,
            'variances': rows, 'dave_queue': queue, 'causes': causes,
            'week_causes': week, 'params': {k: v for k, v in rp.items()},
            'answerers': {'PC': rp['pc_owners'], 'SALES': rp['sales_owners']},
            'first_day': make_serializable(rng.get('lo')),
            'last_day': make_serializable(rng.get('hi'))}
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
    conn.commit()
    return {'status': 'success', 'warning': warn}
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