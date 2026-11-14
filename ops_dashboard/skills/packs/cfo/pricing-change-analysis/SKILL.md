---
name: "pricing-change-analysis"
description: "Use when changing a price, adding a tier or discounting, to model the revenue effect and the churn increase that would cancel it."
---

# Pricing Change Analysis

Models one price change before it ships. Ends with a recommended price, a
break-even churn assumption, and the grandfathering decision.

## When to use

- Raising or lowering a price, adding or retiring a tier, offering a
  discount, or reacting to a competitor's price change.

Not for: the economics of the current price (use unit economics review), or
positioning against competitors (use competitive positioning).

## Step 1 - State the change precisely

- Which plan, from what price to what price, in what currency.
- Effective date, and how existing customers are treated.
- Whether it is a one-off experiment or permanent, and the population it
  applies to: all customers, new customers only, or self-selected upgrades.

A price change without an effective date and a grandfathering rule is not
decided; it is only intended.

## Step 2 - Model the revenue effect

Let N be current customers on the plan, P the current price, and c the
monthly churn rate on that plan.

- MRR on the plan before the change = N x P x (1 - c).
- MRR after the change, churn unchanged = N x (1 - c) x P_new.
- Net new MRR = the uplift, MINUS the MRR lost to extra churn.

Always model the churn effect. A 20% price rise that adds 5 percentage points
of monthly churn to a plan with 5% churn is a net loss.

Break-even churn: the old MRR is preserved when the relative price increase
equals the relative revenue lost to extra churn. For a 20% rise, churn may
rise from 5% to about 6.1% and still break even; from 5% to 8% does not.

State the break-even as a number, then the churn assumption used, and where
it comes from: a past change, a comparable plan, or a stated guess.

## Step 3 - Model the volume effect

- A 20% price rise can lose at most 16.7% of new customers before new
  business MRR falls (1 / 1.2 = 0.833).
- If the change applies only to new customers, existing MRR is untouched:
  model a 3-month cohort comparison, not the whole base.
- If the change adds a tier, model cannibalisation: how many current
  customers move up (revenue positive), how many move down (revenue
  negative), how many stay. Cannibalisation above 40% of the new tier's
  signups means the tier is a discount, not an upgrade.

## Step 4 - Decide the grandfathering rule

Grandfather by default, and grandfather generously:

- Existing annual customers: keep the old price until renewal, then move
  them to the new price at the next renewal date.
- Existing monthly customers: offer the new price at the next billing cycle
  with at least 30 days notice, or move them to a legacy tier at the old
  price.
- Never surprise a customer at renewal with a change they did not see
  coming; churn from that alone can exceed the uplift for months.

Never grandfather forever: a permanent legacy tier needs a sunset date, or
it becomes a permanent discount that compounds.

## Step 5 - Check the consequences and instrument

- Does the payment provider need a new price ID, and does that need a code
  change? Plan the lead time.
- Does the app store take a cut on the higher price, and does tax handling
  change at a new price point?
- Do marketing pages, comparison tables, and live ads quote the old price?
  List every surface that names it, and the order of edits.
- Is there a support script? Write the two sentences support will say.

Recommended change: one price, one date, one grandfathering rule, not three
options in the memo. Record the baseline before launch (MRR on the affected
plan, monthly churn, ARPA, trial-to-paid conversion), then check 30, 60 and
90 days after the effective date:

- MRR up but total customers down more than expected: the price is too high
  for the segment.
- Churn up more than the break-even: the price exceeds what the segment
  values.
- Conversion dropped more than the price rose: the messaging did not justify
  the change.

## What a good answer looks like

One page: the change in one line with the effective date; a before/after
table (price, customers, churn assumption, MRR); the break-even churn and
the assumption used; the volume and cannibalisation effect; the
grandfathering rule and notice period; the surfaces quoting the price; the
baseline metrics and the 30/60/90-day checkpoints; and a rollback naming the
price and the signal that restores it.

## Common traps

- Modelling the price rise without the churn it causes; most of them lose
  money that way.
- Raising the price for everyone at once when new customers only would give
  the same cash with less risk.
- Ignoring cannibalisation when adding a tier, or grandfathering forever.
- Changing the price and the packaging in the same move, so neither effect
  can be read.
- Announcing a rise while the marketing pages still quote the old number.
