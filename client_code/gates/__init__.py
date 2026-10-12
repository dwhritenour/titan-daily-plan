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

A gated order still shows in the candidate list with a stop sign, but the plan will not propose it and PC has to add it on purpose. There are eight gates (two added 10/12, from the 10/10 decisions). "On the shelf" always means the physical count at Starlite; what e21 has allocated to an order counts for nothing either way (allocation is bookkeeping that PC moves around, so since 10/12 the gates and the plan ignore it).

| Gate | What it means in plain words | How it clears | Rule last changed |
|---|---|---|---|
| **Order on hold** | The order is on hold in e21 (credit, quality, customer request). It cannot ship as things stand. | The hold is released in e21. | 8/31 (since the start) |
| **No stock, nothing coming** | Zero on the shelf for this item and no purchase order on the way (stock at the Hub is named in the note as a transfer candidate). Since 10/12 it looks at the line's full quantity, whatever e21 shows allocated. For strainers and foot valves the element can still be built; the body cannot. | Stock arrives, or a PO is placed (then it becomes "due in"). | 10/12 |
| **No stock, due in** | Zero on the shelf, but a PO is on the ocean (with a date) or released to a vendor (no trusted date yet). PC can still add it on purpose to pre-build the element while the body is on the water. Same full-quantity test as "nothing coming" since 10/12. | The PO is received. | 10/12 |
| **Stock promised to another order** (valves only) | Units are on the shelf, but e21 has committed them to other orders and not enough are left for what this line still needs. A reallocation decision, not a dead end. Strainers do not get this gate (decided 10/10); for them the plan itself settles who gets the shelf (see "two orders, one shelf" below). | PC reallocates in e21, or the other orders ship. | 10/12 |
| **Material hold** | PC's answer to a retro question says the material for this line is not here ("waiting for castings", "on PO 785022"). Since 10/12 the hold goes on the moment the answer is read that way, with the expiry taken from the date in the answer; Dave reviews after the fact and can undo it. Dave can also place one by hand from the review page. | PC releases it from the flag popup, the date in the answer arrives, it times out, or Dave's review reads the answer differently. | 10/12 |
| **No work order on file** | The order has no work order in our system. Almost always a data glitch, not a real condition. | The work-order feed catches up (usually within 15 minutes). | 8/31 (since the start) |
| **Short stock** (since 10/12) | The shelf count is above zero but below the line's quantity: "on hand 1 of 89". Strainers and the other non-valve items (valves carry "promised to another order" instead). PC can still add it on purpose for the quantity on the shelf (a deliberate partial), and the plan carries that quantity. | Stock arrives, or PC adds a partial. | 10/12 |
| **Stalled** (since 10/12) | The line sat on the plan three plan days in a row with no shipment and no work-order activity. It leaves the plan and PC gets one question for the order: what is it waiting on, and when? | PC answers with a date (the whole order comes back that day), or releases it from the flag popup, or five business days pass. | 10/12 |

Everything else on an order is a priority signal, not a gate: already late, due today or tomorrow, expedited ship method, high-dollar order. Those decide how high an order ranks; they never stop it.

Two things that look like gates but are not. A line too big for the day is not gated; PC is asked for a quantity when adding it, and the plan carries "SPLIT 20 of 89". And two orders wanting the same shelf stock is not a gate either: both go on the plan, the second at zero with a "⚖ shelf decision" note, for PC and Sales to settle before publish (item 7 below).

### What warned but did not stop: the gap (as presented 10/10)

*Kept as presented. The short-stock warning described here became a gate on 10/12 (Recommendation 1 and item 1 below); the table shows why.*

The stock gate fired on exactly one condition: zero on the shelf. One unit on hand and it stayed quiet. "Out of stock" in PC's words usually means "not enough" or "no castings", and the orders that had been sitting showed the gap.

| Order / item | On the shelf | Needed (all open orders) | What PC said |
|---|---|---|---|
| 602441, 602442, 602443 / YS58D1600 | 1 | 89 | Not enough casting; waiting for stock |
| 603282 / BS85LS1000 | 1 | 33 | (the line that started the short-stock flag) |
| 599024 / YS56BZ0050 | 150 | 840 | Not enough casting for this order |
| 600450 / YS61C0400 | 19 (all promised elsewhere) | 304, with 654 on a PO | Out of stock |
| 609614 / YS62S0200 | 4 | 19 | (no answer yet) |

From 9/25 to 10/12 these showed a **short stock** warning on the candidate row: a triangle that said "on hand 1 of 89". We made it a warning rather than a gate on purpose, to see how it would go. The plan still proposed the line because it was late and valuable, and the warning was easy to miss at 6:30 in the morning.

When PC says "not enough casting", that is the finished unit itself (everything we receive is finished product; we modify it, for example with special straining elements). The short-stock warning already saw exactly that: 1 on hand of 89. One thing the rules saw only for valves was the "promised to another order" gate; a strainer whose shelf stock was wanted by other orders slid through (settled by item 7: the plan now allocates the shelf by priority).

Not every sitting line is a stock problem. 598129 and 600386 / YS80TS0075 have 1,598 on hand; that is a 1,400-piece job queued behind another, and a line that size needs to be planned a day's worth at a time (that change is already built). And 610126 / YS58UDG0200, a two-inch expedited line with stock for it, has been untouched for thirteen days and deserves a direct question.

### Recommendation 1: make short stock a gate — *decided yes 10/10, in place 10/12*

Turn the short-stock warning into a gate: if the shelf count is below what the line needs, the plan does not propose it. The row would read "short stock, on hand 1 of 89" with the stop sign instead of the triangle.

What it would do today: 73 open lines carry the warning, 9 are already stopped by another gate, so about 64 lines move from "proposable" to "gated". Seven of the twelve worst repeat offenders are among them.

What stays in PC's hands: the same override the "due in" gate has. PC can still add a gated line on purpose, to pre-build the element or to ship a partial, and the plan records that it was a deliberate choice.

What it does not fix: stock that is on the shelf but already promised to another order. That gate applies to valves only today (decision 4 below).

### Recommendation 2: stop re-proposing lines that are stuck — *decided yes 10/10, both parts in place 10/12*

Two changes that work together.

**A line untouched three days in a row leaves the plan on its own.** After the third plan day with no shipment and no work-order activity, the plan stops proposing it and sends PC one question: what is it waiting on, and when? It comes back when PC answers with a date (or releases it), not before. Today the same line is proposed, and asked about, day after day: the three YS58D1600 orders (602441, 602442, 602443) have been on the plan five to seven days each, and PC has answered the same question about them four times since 9/23.

**A material answer takes effect right away.** When PC answers "waiting for castings" or "units on PO 785022", the line gets a material hold the moment the answer is read as material short with reasonable confidence, instead of waiting for Dave to confirm the reading on the review page. Dave still reviews, after the fact, and can undo a hold. Today the hold exists in the system but only lands after that confirm step, which has been the bottleneck; 22 answers are still waiting on it.

Together with Recommendation 1, this takes most of the repeat list out of the morning proposal and leaves the meeting with lines that can actually be built today.

### For the room to decide

Struck through = decided and in place.

1. ~~**Short stock becomes a gate** (yes / no). If yes: gate when the shelf count is below the line's quantity, or only when it is below some share of it?~~
   *Decided yes, 10/10. In place 10/12: the shelf count below the line's quantity gates (X08), no percentage; PC overrides from the flag popup with a quantity.*
2. ~~**A line untouched three plan days in a row leaves the plan** until PC answers with a date (yes / no; three days, or a different number?)~~
   *Decided yes, three days, 10/10. In place 10/12: the retro places the hold, one question per order, the answer's date brings it back.*
3. ~~**A material answer places the hold immediately**, with Dave reviewing after the fact (yes / no)~~
   *Decided yes, 10/10. In place 10/12: a material answer read as Probable or better places the hold at once; Dave's review can undo it.*
4. ~~**Should "stock promised to another order" apply to strainers too**, or stay valves only?~~
   *Decided no, 10/10: valves only. On-hand is the right number for strainers; the short-stock gate covers the real shortage.*
5. ~~**When a gated line is added on purpose for a partial**, should the plan carry the quantity PC intends to ship, so Sales can tell the customer?~~
   *Decided yes, 10/10. In place 10/12 (evening): any plan line can be planned at a quantity from the star dialog; the Qty column reads "20 of 89"; the Flag filter has "Partial quantity" so Sales sees every partial in one place.*
6. ~~**610126 / YS58UDG0200**: why has a two-inch expedited line with stock sat for thirteen days?~~
   *Taken care of 10/12.*
7. ~~**Two orders, one shelf** (new): two Must lines for the same item on the same day, where the shelf covers either but not both. Today both are proposed and one sits.~~
   *Decided yes, 10/10; Production's version 10/12: both orders go on the plan. The higher-priority order is planned for the stock; the other line shows on the board at 0 in with a "⚖ SHELF DECISION" note naming it. Before publish, PC and Sales decide: remove it (ship the other complete) or plan a quantity on each (item 5). Undecided at publish, it leaves the plan and the priority pick stands. When the shelf covers part of the second line it is proposed for that part ("SPLIT 20 of 89 - stock: the rest is planned on order N").*

*Prepared for the PC + Sales meeting, Friday 10/10/2026. Numbers as of 10/8. Gate table and decisions updated 10/12 (evening).*
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