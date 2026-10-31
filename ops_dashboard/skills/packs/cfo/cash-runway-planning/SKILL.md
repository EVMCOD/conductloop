---
name: "cash-runway-planning"
description: "Use when planning spending, deciding on a raise, or judging how long the money lasts, to compute runway and the scenarios to plan for."
---

# Cash Runway Planning

Computes how long the money lasts, then plans for the three scenarios that
actually happen. Ends with a number, a trigger, and a decision.

## When to use

- Monthly: check the balance and the burn, and update the runway number.
- Before any spend above a threshold: a contractor, an annual plan, a
  campaign, a device, a lease.
- When deciding whether to raise, and how much.

Not for: unit-level profitability (use unit economics review), or choosing
what to build (use portfolio prioritization).

## Step 1 - Compute the burn

Net burn = (cash out) - (cash in), monthly.

- Use the trailing 3 months, not the last month: one unusual invoice or a
  launch spike distorts the current month.
- Separate committed burn from discretionary. Committed: hosting,
  subscriptions, contractor retainers, the app store's cut, taxes. Here
  "committed" means a recurring obligation, not merely a large one.
- Cash in is cash collected, not revenue. Deferred annual payments and
  unpaid invoices are not cash. If collection is slow, note the gap.
- If cash in exceeds cash out, the business is cash-flow positive and the
  question becomes what to do with the surplus.

## Step 2 - Compute the runway

Runway in months = cash balance / monthly net burn.

Then adjust the balance for anything that lands soon but is not yet paid:
unpaid invoices and taxes owed (a quarter of self-employment tax is a common
miss), known annual renewals (domains, certificates, insurance, licences),
and capex committed but not yet invoiced.

A runway computed on the bank balance alone, when a tax bill is due next
month, is wrong by the size of the tax bill.

## Step 3 - Run the three scenarios, always

Never plan one scenario. Always these three, in a table:

| Scenario | Assumption | Runway |
|---|---|---|
| Base | Current burn, current revenue | X months |
| Downside | Revenue drops 30%, burn unchanged | Y months |
| Cost cut | Revenue unchanged, discretionary burn cut 40% | Z months |

Add a fourth whenever relevant: a hire or spend that adds N per month from
month M, with its runway.

Decision rule: if the downside scenario is under 6 months, the business has a
financing problem regardless of what the base case says. Start the raise or
the cost cut now, not when the base case breaks.

| Runway | Band | Action |
|---|---|---|
| Over 12 months | Healthy | Do not raise out of anxiety. Spend on what raises revenue. |
| 6 to 12 months | Plan | A raise takes 3 to 6 months to close. Set the target amount and a list of 5 investors now. |
| 3 to 6 months | Urgent | Cut discretionary burn by 40% this month; start the raise this week. |
| Under 3 months | Emergency | Cut to the committed floor now, raise immediately, prioritise whatever collects cash fastest. |

## Step 4 - Size a raise, if raising

Two ways to size it; take the larger.

- Runway extension: amount = (target runway in months - current runway) x
  monthly net burn. Target 18 months for a product with no external
  pressure, 12 if the market is moving fast.
- Milestone amount: the money needed to reach one specific, datable milestone
  (a revenue level, a customer count) plus 6 months of buffer.

Then the dilution reality check: raising R at valuation V costs the founder
R / (V + R). At a 10M valuation, raising 1M costs 9% of the company; at 2M
it costs 33%. For a solo founder's small products the alternatives are
revenue, a small loan, or cutting scope: say which the numbers favour, and
what happens at a lower valuation.

## Step 5 - Stress the plan

- What single event halves revenue? A churn spike, a platform policy, a
  payment provider change, one big customer leaving. If 30% of revenue is one
  customer, that customer is the plan.
- What breaks if the raise takes twice as long as planned? Model a 9-month
  raise closing in month 4 instead of month 7.
- Does any cost scale with usage? A per-request API, a per-seat plan, a cut.

## What a good answer looks like

One page: cash balance adjusted for known unpaid bills, with the date;
monthly net burn on a 3-month trailing basis, split committed and
discretionary; runway in months for the base case; the three-scenario table;
the threshold band and the action it implies; if raising, the target amount,
the milestone it funds, dilution at the current valuation and the 5 names to
contact; the one expense deferred because of this plan; and the one spending
item still worth doing.

## Common traps

- Using one month of burn, which makes a good month look like a crisis.
- Counting revenue as cash, especially with annual prepayment.
- Forgetting taxes and annual renewals, a quarter of the runway.
- Raising on the base case when the downside case is already uncomfortable.
- Sizing the raise by what is comfortable to sell rather than what the
  milestones cost.
- Cutting only fixed costs; discretionary cuts are usually faster.
