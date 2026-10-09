from ._anvil_designer import gatesTemplate
from anvil import *
import anvil.server


# =============================================================================
# gates - "What keeps an order off today's plan" (2026-10-09, Dave)
#
# A page INSIDE the Daily Plan app (<app-url>#gates, and the 'Gates' link in
# the plan header) so the room can read the gate rules and the two
# recommendations on the same browser tab Dave is already sharing.
#
# Designer components (hand-added, hasattr-guarded like the other forms):
#   lk_plan    Link      '← Daily Plan'
#   lbl_title  Label     'What gets gated'
#   rt_body    RichText  format = markdown; the text below
#
# The text is plain-language by design: no flag codes, no table names. The
# numbers are as of 2026-10-08 (09-14 .. 10-07) and are restated here, not
# computed - this is a handout, not a report. Edit the string, paste, done.
# =============================================================================

BODY = """
### Why we are talking about this

The faith is dropping for a reason the data doesn't support. The floor is producing at capacity (106% by work orders done) and shipping at 91% of it. What's broken is the plan's content: it keeps naming lines that can't be built, so the meeting picks "important" correctly and then watches those lines sit for a week because the castings aren't here.

The numbers behind that, 9/14 through 10/7: 423 planned line-days showed no activity, but those were only 173 different lines. 55 of them were on the plan three days or more in a row, a dozen for six days or longer. When PC was asked about them, the answers were the same each time: not enough castings, out of stock, units on a PO that hasn't arrived, a bigger order ahead of this one.

So the question for today is not whether the plan is worth doing. It is which conditions should keep an order off the plan until it can actually be built.

### What keeps an order off today's plan (the gates)

A gated order still shows in the candidate list with a stop sign, but the plan will not propose it and PC has to add it on purpose. Today there are six gates.

| Gate | What it means in plain words | How it clears |
|---|---|---|
| **Order on hold** | The order is on hold in e21 (credit, quality, customer request). It cannot ship as things stand. | The hold is released in e21. |
| **No stock, nothing coming** | Zero on the shelf for this item and no purchase order on the way. For strainers and foot valves the element can still be built; the body cannot. | Stock arrives, or a PO is placed (then it becomes "due in"). |
| **No stock, due in** | Zero on the shelf, but a PO is on the ocean (with a date) or released to a vendor (no trusted date yet). PC can still add it on purpose to pre-build the element while the body is on the water. | The PO is received. |
| **Stock promised to another order** (valves only) | Units are on the shelf, but e21 has already committed them to other orders and not enough are left for this one. A reallocation decision, not a dead end. | Someone decides which order gets the stock. |
| **Material hold** | PC answered a retro question saying the material for this line is not on hand. The line stays off the plan until PC releases the hold or it expires. | PC releases it from the flag popup, or it times out. |
| **No work order on file** | The order has no work order in our system. Almost always a data glitch, not a real condition. | The work-order feed catches up (usually within 15 minutes). |

Everything else on an order is a priority signal, not a gate: already late, due today or tomorrow, expedited ship method, high-dollar order. Those decide how high an order ranks; they never stop it.

### What warns but does not stop: the gap

The stock gate fires on exactly one condition: zero on the shelf. One unit on hand and it stays quiet. "Out of stock" in PC's words usually means "not enough" or "no castings", and the orders that have been sitting show the gap.

| Order / item | On the shelf | Needed (all open orders) | What PC said |
|---|---|---|---|
| 602441, 602442, 602443 / YS58D1600 | 1 | 89 | Not enough casting; waiting for stock |
| 603282 / BS85LS1000 | 1 | 33 | (the line that started the short-stock flag) |
| 599024 / YS56BZ0050 | 150 | 840 | Not enough casting for this order |
| 600450 / YS61C0400 | 19 (all promised elsewhere) | 304, with 654 on a PO | Out of stock |
| 609614 / YS62S0200 | 4 | 19 | (no answer yet) |

Since 9/25 these show a **short stock** warning on the candidate row: a triangle that says "on hand 1 of 89". We made it a warning rather than a gate on purpose, to see how it would go. The plan still proposes the line because it is late and valuable, and the warning is easy to miss at 6:30 in the morning.

When PC says "not enough casting", that is the finished unit itself (everything we receive is finished product; we modify it, for example with special straining elements). The short-stock warning already sees exactly that: 1 on hand of 89. One thing the rules see only for valves is the "promised to another order" gate; a strainer whose shelf stock is already committed to other orders slides through.

Not every sitting line is a stock problem. 598129 and 600386 / YS80TS0075 have 1,598 on hand; that is a 1,400-piece job queued behind another, and a line that size needs to be planned a day's worth at a time (that change is already built). And 610126 / YS58UDG0200, a two-inch expedited line with stock for it, has been untouched for thirteen days and deserves a direct question.

### Recommendation 1: make short stock a gate

Turn the short-stock warning into a gate: if the shelf count is below what the line needs, the plan does not propose it. The row would read "short stock, on hand 1 of 89" with the stop sign instead of the triangle.

What it would do today: 73 open lines carry the warning, 9 are already stopped by another gate, so about 64 lines move from "proposable" to "gated". Seven of the twelve worst repeat offenders are among them.

What stays in PC's hands: the same override the "due in" gate has. PC can still add a gated line on purpose, to pre-build the element or to ship a partial, and the plan records that it was a deliberate choice.

What it does not fix: stock that is on the shelf but already promised to another order. That gate applies to valves only today (decision 4 below).

### Recommendation 2: stop re-proposing lines that are stuck

Two changes that work together.

**A line untouched three days in a row leaves the plan on its own.** After the third plan day with no shipment and no work-order activity, the plan stops proposing it and sends PC one question: what is it waiting on, and when? It comes back when PC answers with a date (or releases it), not before. Today the same line is proposed, and asked about, day after day: the three YS58D1600 orders (602441, 602442, 602443) have been on the plan five to seven days each, and PC has answered the same question about them four times since 9/23.

**A material answer takes effect right away.** When PC answers "waiting for castings" or "units on PO 785022", the line gets a material hold the moment the answer is read as material short with reasonable confidence, instead of waiting for Dave to confirm the reading on the review page. Dave still reviews, after the fact, and can undo a hold. Today the hold exists in the system but only lands after that confirm step, which has been the bottleneck; 22 answers are still waiting on it.

Together with Recommendation 1, this takes most of the repeat list out of the morning proposal and leaves the meeting with lines that can actually be built today.

### For the room to decide

1. **Short stock becomes a gate** (yes / no). If yes: gate when the shelf count is below the line's quantity, or only when it is below some share of it?
   *Recommendation: yes, and gate whenever the shelf count is below the line's quantity. No percentage; a partial is a deliberate PC override, not a rule.*
2. **A line untouched three plan days in a row leaves the plan** until PC answers with a date (yes / no; three days, or a different number?)
   *Recommendation: yes, three days. Two is too twitchy for a Monday; five is a week of asking the same question.*
3. **A material answer places the hold immediately**, with Dave reviewing after the fact (yes / no)
   *Recommendation: yes. The hold is reversible and the review page stays; waiting on the confirm step is what let the same lines come back for a week.*
4. **Should "stock promised to another order" apply to strainers too**, or stay valves only?
   *Recommendation: no, valves only. e21 commits every open order's quantity on strainers, so the test would have gated about 493 lines that had stock on the shelf (9/25 count). On-hand is the right number for strainers; the short-stock gate covers the real shortage.*
5. **When a gated line is added on purpose for a partial**, should the plan carry the quantity PC intends to ship, so Sales can tell the customer?
   *Recommendation: yes. The plan already carries a planned quantity for big lines that are split across days; the override add uses the same field.*
6. **610126 / YS58UDG0200**: why has a two-inch expedited line with stock sat for thirteen days?
   *Recommendation: ask Tara and Kim in the room; no rule change until we know.*
7. **Two orders, one shelf** (new): two Must lines for the same item on the same day, where the shelf covers either but not both. Today both are proposed and one sits.
   *Recommendation: yes, add it. The proposal keeps a running count per item and stops at on-hand, marking the second line "short, stock planned on order N". That gives strainers the allocation decision by priority instead of by e21's commit order.*

*Prepared for the PC + Sales meeting, Friday 10/10/2026. Numbers as of 10/8.*
"""


class gates(gatesTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    if hasattr(self, 'lk_plan'):
      self.lk_plan.text = '← Daily Plan'
      self.lk_plan.set_event_handler('click', self._go_plan)
    if hasattr(self, 'lbl_title'):
      self.lbl_title.text = 'What gets gated, and why planned orders sit'
    if hasattr(self, 'rt_body'):
      self.rt_body.format = 'markdown'
      self.rt_body.content = BODY
    else:
      # no designer component yet: still show the text
      self.add_component(RichText(content=BODY, format='markdown'))

  def _go_plan(self, **event_args):
    set_url_hash('')
    open_form('ship_plan')