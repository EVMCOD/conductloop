---
name: "roadmap-prioritization"
description: "Use when several candidate features or projects compete for a quarter, to score them on impact, effort and confidence, commit only what fits capacity, and publish what is not being done."
---

# Roadmap Prioritization

Turns a pile of requests into one committed quarter and an explicit list of refusals. A roadmap without a "not
doing" list is a wish list, and the wish list always wins because it is never shown.

## When to use

- More than 3 candidate items could go into the next 6 to 12 weeks.
- A quarter is being planned and capacity is finite.
- Requests keep arriving faster than they ship, and something has to give.
- A product feels busy but nothing measurable moved.

Not for: the internal shape of one feature (that is a PRD), or a customer's one-off request (answer it, then
score it later).

## Step 1 - Build the candidate list

One row per candidate, capped at 10. If more than 10 are live, the list itself is the problem: group them into
themes and score the themes first.

Each row carries:

- The outcome in one sentence, not the feature ("reduce time to first report", not "add a report builder").
- The affected users or accounts, with a number, not a vibe.
- Evidence tier: measured, cited, or assumption. Mark assumptions in caps.
- A rough size in person-days, from the estimate, not from hope.

Include the "do nothing" option as a row. If shipping nothing scores best, that is a legitimate result and the
quarter goes to support and maintenance.

## Step 2 - Score on three weighted factors

Each factor is scored 1 to 5. The weights are fixed and do not move per quarter.

| Factor | Weight | 1 means | 5 means |
|---|---|---|---|
| Impact | 0.40 | affects almost nobody, or nobody can tell | moves revenue, retention or a blocked conversion by 5% or more |
| Confidence | 0.30 | one opinion, or an analogy to another product | measured in the product's own data, or 5+ interviews, or pre-commitment |
| Effort (inverse) | 0.30 | more than 20 person-days | 2 person-days or fewer |

Weighted score = 0.40 x Impact + 0.30 x Confidence + 0.30 x Effort.

Impact in detail: 5 = at least 5% change in revenue, churn or a funnel step that is currently blocked, or it
unlocks a deal that is stuck. 3 = a real but second-order improvement for a subset. 1 = cosmetic or
internal-only. Never score impact on potential; score it on expected effect with today's evidence.

Confidence in detail: 5 = a number in the product's own telemetry or a signed pre-commitment. 4 = 5 or more
independent interviews with a consistent pattern. 3 = repeated support tickets or 2 interviews. 2 = one
anecdote. 1 = a hunch, including the founder's own.

Effort in detail: estimate in person-days for design, build, test, release and support. 5 = 2 days, 4 = 3 to 5
days, 3 = 6 to 10 days, 2 = 11 to 20 days, 1 = more than 20 days or unknown. If it is unknown, it is 1.

## Step 3 - Apply the thresholds

- Score 3.6 or higher: candidate for the quarter.
- Score 2.8 to 3.59: needs one specific missing fact before it can be scheduled. The memo names that fact, who gets it, and by when.
- Score below 2.8: not scheduled. Say so explicitly so it stops returning every week.

Confidence override: an item with confidence 1 or 2 never enters a quarter on its score alone. It enters only
as a spike of at most 3 person-days whose purpose is to raise its confidence, and the spike is scored as its
own candidate.

Capacity rule: estimate the quarter's real capacity in person-days (people x working days x 0.6 for support
and interrupts), then commit items until the sum reaches at most 80% of that capacity. The remaining 20% is
the buffer for the support load that always arrives. Overcommitting at 100% is the single most common cause of
a quarter that ships nothing.

## Step 4 - Write the not-doing list

This is a deliverable, not an internal note. One entry per rejected or deferred item:

- The item in one line.
- The score or reason it did not make it ("score 2.1", "confidence 1", "no capacity left").
- What would change the answer: the evidence, the trigger, or the date to revisit. A deferred item without a
  trigger comes back every week.
- A one-sentence reason the founder can repeat to the person who asked.

Write the list in the same order the requests came in, so each requester finds their own item. Include items
that lost to something else, not only items that scored low.

## Step 5 - Tie-breaks and re-scoring

- Two items within 0.2 points: take the one with lower effort. If effort is equal, take the one with higher
  confidence.
- A committed item that does not ship in 2 weeks is re-scored with its actual remaining effort, not its
  original estimate.
- Review the list every 4 weeks. Do not re-score weekly; weekly re-scoring means nothing ever ships.
- Re-score when a factor changes materially: a churn spike, a pricing change, a new competitor, or a
  dependency landing.

## What a good answer looks like

- A candidate table with impact, confidence, effort, and the weighted score.
- A committed list that fits 80% of stated capacity, with the capacity number written down.
- A not-doing list where every entry has a reason and a trigger, phrased so it can be pasted into a reply to
  the requester.
- A spike for anything low-confidence that still deserves a slot.
- One sentence on what the quarter deliberately does not attempt.

## Common traps

- Scoring a list where three items are 5 and everything else is 1; that is a wish list with arithmetic.
- Letting effort be the soft factor. A 20-day item at impact 5 is still usually wrong for a solo founder.
- Committing at 100% of capacity, then discovering support and bugs.
- Writing the not-doing list as internal notes nobody sees, so the same request returns next week.
- Deferring without a trigger, which converts a decision into a deferral forever.
- Re-scoring every week and changing the ranking without shipping anything.
- Treating a high impact score as permission to skip the evidence step.
