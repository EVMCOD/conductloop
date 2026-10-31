---
name: "technical-debt-triage"
description: "Use when a list of technical debt must be ranked, or a refactor request needs a decision based on blast radius and cost instead of ugliness."
---

# Technical Debt Triage

Ranks debt by what it costs, not by how ugly it looks. Ends with a ranked
table, what to fix now, what to schedule, and what to consciously accept.

## When to use

- A backlog of "we should refactor this" items needs an order.
- A change takes 3x longer than it should, and the reason is unclear.
- A dependency is aging and something needs to be done about it.
- Deciding whether to spend a week on a rewrite, or on the roadmap.

Not for: a single known bug (use code review rigor), or choosing which
feature to build (use portfolio prioritization).

## Step 1 - Make the debt concrete

An item is not debt until it has a symptom. For each candidate, write the
symptom in one line:

- "Every new feature needs 3 days of plumbing before it ships."
- "The test suite takes 40 minutes, so it gets skipped."
- "Two products share a copy of the same module; the last fix was applied to
  one of them."

If you cannot write the symptom, it is a style preference, not debt. Delete
it from the list. Roughly half of a typical list is style preferences.

Score each item:

| Field | Scale | Notes |
|---|---|---|
| Blast radius | 1 to 5 | 1 = one function, 3 = one product, 5 = every product |
| Frequency | 1 to 5 | how often the symptom is paid for (per week) |
| Time to fix | hours | the real number, including the tests |
| Urgency | 0 to 2 | 0 = none, 1 = a deadline, 2 = a customer is affected |

Risk score = blast radius x frequency. This is the ranking key: it measures
how much damage the debt does per week, which is what matters. Ugliness does
not appear in the formula.

## Step 2 - Triage into four buckets

Apply the thresholds to the risk score:

- FIX NOW (risk 15+, or blast radius 5): do it this week, before new work.
  A blast-radius-5 item is always now: it is shared by every product.
- SCHEDULE (risk 8 to 14): put it in the next 4-week block, with the date
  and the hours in the plan. An unscheduled item does not get done.
- PAY DOWN (risk 4 to 7): fix opportunistically, inside work that already
  touches that code. Do not schedule a dedicated session.
- ACCEPT (risk under 4): write down why it is being accepted and what would
  change that. Accepting debt on purpose is a decision; ignoring it is not.

Any item with an active customer impact (urgency 2) moves one bucket up,
whatever the score.

## Step 3 - Apply the 20% rule

Debt work gets at most 20% of the engineering time in a given month. When
debt exceeds that, the fix is to stop adding scope, not to spend more time
on debt. A founder with 5 live products cannot spend half the month on
cleanup.

Exception: a debt item with blast radius 5 (shared across products) or an
active customer impact overrides the cap. Those are not debt, they are
outages waiting for a schedule.

## Step 4 - Keep the list small and current

- Cap the working list at 7 items. A list of 30 is a wish list.
- Every item needs an owner, a bucket, and a date or a trigger.
- Items in SCHEDULE that slip twice get re-triaged, not rescheduled: either
  the estimate was wrong or the priority was wrong, and both are worth
  knowing.
- After each fix, close the item with the symptom it removed and the date.
  Otherwise the same item comes back next quarter.

## What a good answer looks like

One page:

- The triaged table: item, symptom in one line, blast radius, frequency,
  risk score, bucket, hours, and the date or trigger.
- The one FIX NOW item, with the hours and the reason it jumped the queue.
- The 20% time budget and how the month is planned around it.
- The ACCEPT list with the reason each was accepted and what would reverse
  it.
- The item that was deleted from the list because it was a style preference.

## Common traps

- Ranking by how ugly the code is, which fixes the code nobody touches.
- Keeping the whole list, so nothing is ever first.
- Calling something debt when it is a missing feature.
- Scheduling debt with no date, so it never happens.
- Treating every rewrite as urgent; a working system is not broken.
- Spending 40% of the month on cleanup and calling it progress.
- Fixing a symptom without removing its cause, and getting it back next
  quarter.
