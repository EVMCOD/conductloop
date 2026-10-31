---
name: "build-vs-buy-decision"
description: "Use when deciding whether to build a capability, buy a service, or borrow an open-source component, before committing founder hours to it."
---

# Build vs Buy Decision

One decision, three options, one written recommendation with an exit path.
Ends with BUILD, BUY, BORROW, or DO NOTHING, and the first step.

## When to use

- A feature needs a capability that could be a service or a library.
- A subscription cost is being proposed or has crept up.
- A dependency just broke and the fix looks like building it yourself.
- Someone proposes rewriting something that works.

Not for: choosing which feature to build (use portfolio prioritization), or
picking a vendor for something already decided to buy.

## Step 1 - Name the capability precisely

One sentence: "we need X to work". Vague needs produce vague decisions, so
write down the actual requirement:

- The one job it does, and the volume (per request, per day, per month).
- The constraints that are real: latency budget, data that cannot leave the
  machine, offline operation, a specific compliance regime, a language.
- Which of those constraints a customer or a law actually requires, and which
  are preferences. A constraint nobody imposes does not justify a build.

## Step 2 - Classify the frequency

The frequency class decides the default.

| Class | Description | Default |
|---|---|---|
| One-off | Done once or a few times a year | Build it, badly, on a script |
| Recurring non-core | Weekly or monthly, not on the critical path | Buy, or use a hosted free tier |
| Recurring core | On every request or every customer | Buy or borrow, and plan to own it if it grows |

The default follows from the economics in step 3: the second-year cost of a
build is roughly 20% of the first year in maintenance. A one-off task never
justifies an annual subscription.

## Step 3 - Compare three-year totals, not prices

Compute all three costs over 3 years, including the founder's time at a
stated hourly rate. Say the rate; do not leave time out.

Build:

- First build: hours x rate.
- Maintenance: 20% of build hours per year, plus keeping up with breaking
  changes in anything it depends on.
- Opportunity cost: the same hours not spent on revenue.
- Exit cost: what it costs to delete (usually low; this is the point).

Buy:

- Subscription or usage fees, including the price at 3x current volume.
- Integration: the real number is 2x to 4x what the vendor's demo suggests,
  for a founder with no dedicated integrator.
- Ongoing: support, billing glue, migration when the vendor changes pricing or
  is acquired.
- Exit cost: data export, migration, and the customers who leave.

Borrow (open source, a library, a fork):

- Integration and upgrade cost: 4x to 10x the original build cost is normal.
- Fork cost: you now own the maintenance, patches, and licence obligations.
- Exit cost: the fork is yours to carry forever.

Rule of thumb: if the 3-year cost of building is under 40 founder hours and
the capability is not core, build it. If it is core and over 200 hours, buy
unless the vendor fails a check in step 4.

## Step 4 - Run the exit test on the vendor

Before committing, answer these in writing. A vendor that cannot answer them
is a risk, whatever the price:

- Can I export all my data in a documented format, on demand, at no cost? If
  not, you do not own the data.
- What is the price at 3x, 10x, and 100x my current volume? Is there a cap?
- What happens if the vendor is acquired, raises prices, or shuts down? Is
  there a notice period?
- Can I self-host it later, and what does that cost in hours?
- Does it fit the deployment model (local-first, offline, no cloud)? A service
  that needs a server breaks a local-first product.
- What is the data residency and the subprocessors list?
- Is the free tier still free at my volume, or is the pricing cliff at my
  exact size?

Lock-in test: if leaving costs more than 40 hours, the vendor is not replaceable.

## Step 5 - Decide and write it down

Recommendation on first line, then: class (one-off, recurring non-core,
recurring core); 3-year cost of each option in table; exit test results with
lock-in figure; risk that dominates (vendor lock-in, unmaintained dependency,
founder's time, scope creep); first step, under 2 hours, plus date to revisit;
and trigger that reverses this decision.

## Common traps

- Comparing a one-time build cost to a monthly price. Compare 3-year totals.
- Forgetting integration hours, which are usually 2x to 4x the demo.
- Ignoring the exit cost, then discovering the data is trapped.
- Choosing a heavyweight service for a one-off task.
- Forking a library for a 10-line fix and inheriting it forever.
- Treating "we can build it" as free, when it is the founder's scarcest
  resource.
- Not writing the reversal trigger, so the decision is never revisited.
