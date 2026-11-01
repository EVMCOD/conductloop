---
name: "portfolio-prioritization"
description: "Use when more than one product or project could be worked on next and the choice must be made with a scored, written decision."
---

# Portfolio Prioritization

Decides what the founder works on next across many products, and when to stop
something. Ends in one ranked order, one chosen item, and a written stop
rule for the runner-up.

## When to use

- More than one project could get the next week of founder time.
- A "should I build this?" question arrives while other work is unfinished.
- Several products are half-alive and nobody can say which deserves time.
- Weekly planning, or a monthly portfolio review.

Not for: deciding what to do inside one product (that is a product planning
decision), or an urgent incident (handle it, then re-prioritize).

## Step 1 - List the candidates

One row per candidate, including the "do nothing" option as a row. Each row
carries: a one-line description, the evidence behind it (measured, cited, or
ASSUMPTION), and the estimated effort in founder hours.

Keep the list to 7 items or fewer. If more than 7 candidates are live, the
portfolio itself is the problem and needs pruning before scoring.

## Step 2 - Score on five weighted factors

Score each 0 to 5, then multiply by the weight and sum. Weights, and why:

| Factor | Weight | 0 means | 5 means |
|---|---|---|---|
| Expected revenue impact | 3 | no plausible revenue effect | affects more than 10% of revenue |
| Evidence strength | 3 | a hunch | paying customers or measured demand |
| Effort (inverse) | 2 | more than 40 hours | under 4 hours |
| Risk reduction | 2 | no new risk | removes a risk that could end a product |
| Time criticality | 1 | no deadline | something breaks in 30 days or less |

Revenue impact and evidence carry the most weight because they are what
protects the portfolio from founder-time spent on taste. Effort is scored
inverse so that a large item needs a large payoff to rank.

Interpretation: a score of 3 is neutral. Below 2 means do not start. At 4 or
above, start now. Between 2 and 4, it needs one more fact before it can be
scheduled, and the memo names exactly which fact.

Do not score an item on its potential. Score it on its expected value with
the evidence you have today.

## Step 3 - Apply the stop rules before the ranking

A portfolio needs endings, not just priorities. Any candidate hits a stop
rule and leaves the list, regardless of score:

- No revenue and no usage in 90 days: stop or archive.
- Maintenance cost above 2 hours a month with no revenue: stop.
- Blocked on a third party you do not control for more than 60 days: stop or
  rebuild around it.
- Feature work on a product with fewer than 5 active users: stop.
- Two consecutive months of no movement and no owner attention: stop.

Stopping is a decision with a date, not a shrug. Write it as: "STOP
[product] on [date]. Reason: [rule triggered]. What stays: [data, repo, page]
so it can be revived."

## Step 4 - Pick exactly one

The output of a prioritization round is one item for the next block of
founder time. Not three, not a ranked list to be worked on in parallel. A
solo founder who starts three things finishes none of them.

Run the next block on the winner for 2 to 4 weeks, then re-run this
procedure with the new evidence.

## Step 5 - Record it

A portfolio note, under one page:

- The candidates and their scores, in a table.
- The winner, with the single reason it won.
- The stop decisions, with dates.
- What was deliberately not started, and what would change that.
- The next review date.

## Decision rules

- Two candidates within 1 point of each other: pick the one that is
  reversible. If still tied, pick the one that unblocks someone else.
- The runner-up gets a date, not a promise: "next, if the winner ships by
  [date]".
- Any candidate scoring below 2 is not scheduled. Say so out loud so it
  stops coming back every week.
- If the winner has been the winner for 6 weeks without finishing, the
  problem is the scope, not the priority. Cut the scope in half.

## Common traps

- Scoring three items at 5 and calling it a ranking.
- Scoring potential instead of expected value.
- Ignoring evidence weight and letting a confident idea outrank a measured
  one.
- Never stopping anything, so the portfolio only grows and everything is
  half-done.
- Re-prioritizing weekly, which means nothing ever gets done.
- Treating maintenance as free; an unmaintained product with a public URL
  is a liability, not an asset.
- Letting the loudest request win over the scoring.
