---
name: "prd-writing"
description: "Use when a feature, fix, or change needs a written spec before build starts, to produce a one-page PRD with evidence, scope, and testable acceptance criteria."
---

# PRD Writing

A PRD is a decision, not a document. It exists so one person can approve or reject a specific scope, and
so two people do not build different things from the same request. Ends with one page and a go / no-go.

## When to use

- A feature is approved in principle and someone must write down what "done" means before code starts.
- Scope is disputed: two people describe the same feature differently.
- A customer request needs a shape before it becomes a promise.
- A quarter of roadmap is being committed and the work is bigger than one sprint.

Not for: a one-line bug fix (write the ticket), a support answer (answer it), or the decision to build at
all (roadmap prioritization comes first).

## Step 1 - Gather evidence before writing a word

A PRD that opens with the solution has already failed. Collect, in order:

- The problem in one sentence, in the user's words, with the trigger: "when X happens, they cannot Y".
- At least 3 pieces of evidence: interview quotes, support tickets, usage data, or a screenshot of the
  broken flow. Fewer than 3 and the top of the PRD is marked ASSUMPTION.
- The size of the group affected: how many users hit this in the last 30 days. If it cannot be measured,
  say so.
- The cost of not fixing it: churn, hours, money, or nothing. "Nothing" is a valid answer and it usually
  kills the request.

Never invent evidence. A missing number is written as "unknown, to be measured by [how]", or the PRD goes
back a step.

## Step 2 - Use the fixed nine sections, in this order

1. **Problem** - one sentence, trigger and blocked outcome. No solution nouns.
2. **Evidence** - the 3+ items, each with a source and a date, plus a line on what the evidence does not
   show.
3. **Goal** - the user-visible outcome, one sentence, measurable.
4. **Non-goals** - at least 2, each naming the tempting adjacent thing that is out of scope ("no bulk
   import in v1").
5. **Scope** - the smallest shippable slice, listed as user-visible behaviours. If a line starts with
   "build", it is too big.
6. **Acceptance criteria** - see step 3.
7. **Risks and unknowns** - at most 3, each with the cheapest test that retires it and a date.
8. **Rollout** - flag, beta cohort, percentage, or all. Name the kill switch and who holds it.
9. **Open questions** - at most 5, each with an owner and a due date.

Sections 2, 4 and 6 are the ones people skip and the ones that cause rework. A PRD without a non-goals
list will grow.

## Step 3 - Write acceptance criteria that can fail

One criterion per line, in this shape:

    Given <precondition>, when <action>, then <observable result>.

Rules:

- At least 4 criteria for a normal feature, and one of them is the error or empty-state path.
- Each criterion is checkable by someone who did not write the PRD, without asking a question. "Works as
  expected" fails this test.
- Each criterion names a number, a state, or a visible artefact: "the dashboard shows 0 rows and the
  empty-state text", not "the dashboard updates".
- Cover the boundaries: empty, one, many, and the largest supported case.
- Criteria describe product behaviour, not implementation. "The retry runs every 5 minutes" is behaviour;
  "we use a cron worker" is not.

## Step 4 - Enforce the length rule

A PRD is one page.

- At most 500 words of narrative, excluding the acceptance criteria list.
- At most 12 acceptance criteria, at most 5 open questions, at most 3 risks.
- If it does not fit on one page, the scope is too big: split it into two PRDs or cut a non-goal. Do not
  shrink the type, do not drop the non-goals.
- The file stays under 110 lines. A PRD that needs a second page needs a decision meeting, not a longer
  document.

## Step 5 - Close with a decision, not a discussion

- **BUILD** - approved, with a named owner and a start date.
- **BUILD AFTER** - approved pending one specific unanswered question, with that question and its owner
  named.
- **DO NOT BUILD** - with the reason and what would change it. This is a valid and common outcome; a PRD
  that can only end in BUILD is a request for a rubber stamp.

## What a good answer looks like

- One page, nine sections, none renamed.
- A problem sentence with no product nouns in it.
- Three or more dated pieces of evidence, plus what they do not show.
- At least two explicit non-goals.
- 4 to 12 acceptance criteria, each in Given/When/Then, each falsifiable.
- A rollout line with a kill switch and a named holder.
- One of the three decisions, written down, with a date.

## Common traps

- Writing the solution as the problem: "we need a CSV export" instead of "finance reconciles invoices by
  hand every month".
- Acceptance criteria that describe code instead of behaviour.
- No non-goals section, which returns as a larger request two weeks later.
- Padding with background the reader already knows.
- Mixing three unrelated changes; that is three PRDs.
- Evidence that is one loud customer. One voice is a hypothesis, three independent ones are a pattern.
- Sending the PRD with the open questions still blank. Blanks mean it is not ready and review wastes
  everyone's time.
- Treating "the founder wants it" as evidence. It is a priority, not a proof; record it in the evidence
  section as a request.
