---
name: "onboarding-flow"
description: "Use when new users or accounts are not reaching first value, to define the activation moment, measure where the flow leaks, and choose the fix or the nudge that unblocks each stall."
---

# Onboarding Flow

Find the step where the user gives up, fix that one step, and know which message reaches the user who is stuck there. The goal is time to first value, not the number of screens in the tour.

## When to use

- Activation is flat or falling while signups hold steady.
- A new flow, a new plan, or a new surface has just launched.
- Trial starts are healthy and paid conversions are not.
- The team cannot state, in one sentence, what the activation moment is.

Not for: fixing a bug that breaks a live flow (that is a support ticket first), and not for a marketplace that needs a different motion per side.

## Step 1 - Define the activation moment

Activation is the first moment the user has experienced the value the product sells. It must be observable, and it must be something the user did, not something the product recorded.

Write it as one sentence: "<specific action> <specific object>", for example "a first report is generated and opened", "the first export completes", "a second user joins the workspace". Then list the other candidates and pick one. Only one activation definition per product, otherwise every cohort number means something different.

Rules:

- If the candidate activation action happens 100% of the time on day 1 and correlates weakly with retention, it is
  onboarding, not activation. Push it further: the first time the user gets the outcome they came for.
- Use the metric to sanity-check the definition: compare retention of activated versus non-activated users for the
  last 90 days. If activation does not separate them, the definition is wrong.
- Record the time to activation in minutes, not just a yes/no flag.

## Step 2 - Map the flow in steps

Write the path from entry to activation as numbered steps. For each step record: what the user does, what the product does in response, and the median time spent.

Then mark each step as required, optional, or skippable. Anything optional that sits before activation is a candidate for removal.

Rules:

- Maximum 3 required steps before activation for a self-serve product, 5 for a business product where an admin sets things up.
- Never require an email confirmation before activation. Confirm by background link and let the user proceed.
- Never require a payment method before the user has seen value, unless the product is inherently a paid service.
- Ask for one thing per step. A form with 9 fields is 9 steps wearing one screen.
- If a step needs data the user does not have yet (a file, an API key, a colleague), either defer it until after
  activation or provide a path that works without it.

## Step 3 - Measure where it leaks

For each step, compute the drop rate: users who reached the step and did not continue, divided by users who reached it. Also track the median time per step and the abandonment by step.

The biggest drop is usually step 2, and it is usually one of these: too many fields, an unclear error, an unexplained wait, or a missing permission. Check the raw events for that step before changing anything.

Benchmarks to act on:

- A step with a drop rate above 30% is a defect to fix this week.
- A step with a drop rate between 10% and 30% is worth one experiment.
- A step below 10% is fine; do not spend time there.
- Overall, activation under 40% of signups in the first session is a product problem, not a copy problem.
- Median time to activation above 15 minutes for a self-serve product means too many steps; above 60 minutes means
  the product is being sold to someone who does not know what to do.

## Step 4 - Choose the fix or the nudge

For each leaking step, pick one of three responses, in this order:

1. **Remove or defer the step.** Best when it exists only for the company's benefit. Deferred steps become
   post-activation prompts.
2. **Make the step trivially completable.** Pre-fill from what is already known, add a progress indicator, allow a
   later save, explain the error in the user's language with the exact next action.
3. **Nudge.** Only when the step is already easy and the user still does not do it.

Nudge rules (email or in-app, one per stall):

- Send at most 2 messages per stall: the first after 24 hours of inactivity on that step, the second after 3 days.
  No third.
- Each message contains: the specific step they stopped at, why it matters in one sentence, one action, and one
  link that resumes exactly at that step (not at the start).
- Subject or header stays under 6 words. Body under 80 words.
- Name the value, not the feature: "Your report is ready to review" beats "Finish setting up your workspace".
- Never nudge a user who has already activated, and stop the sequence the moment they activate.
- Measure the nudge: recovery rate of nudged users versus the control group. Under 5% recovery, the nudge is not
  working; fix the step instead.

## Step 5 - Review and report

Weekly, 20 minutes, one page:

- Activation rate this week versus last week and the 4-week average.
- Time to activation, median and 90th percentile.
- The one step with the largest drop, its drop rate, and the decision (remove, simplify, nudge, accept).
- Which nudges were sent, recovery rate, and which were stopped.
- The next single change, with an owner and a date.

Decision rule: one change at a time. Fixing two steps in the same week makes the measurement unreadable, and neither gets the credit.

## What a good answer looks like

- One activation sentence, with the measured retention difference that justifies it.
- The numbered step map, with required or optional marked, 3 to 5 steps.
- A drop rate and median time per step, with the biggest leak named.
- One response per leak (remove, simplify, nudge), and for nudges the timing, the copy and the measured recovery
  rate.
- One change committed for this week, with an owner and a date.

## Common traps

- Defining activation as account creation or app open, then optimising for the wrong metric.
- Adding a step that "the business needs" before the user has seen value.
- Treating a long onboarding tour as onboarding progress.
- Sending a nudge to everyone instead of the users stalled at a specific step.
- Nudging repeatedly, training users to ignore the channel.
- Fixing every leaking step at once, then learning nothing.
- Improving activation while the product is broken for a subset of users.
