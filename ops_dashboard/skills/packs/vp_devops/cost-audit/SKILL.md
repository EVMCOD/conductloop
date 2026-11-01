---
name: cost-audit
description: Use when the infrastructure bill grew, before a month closes, or before adding a new service, to compute cost per business unit, find the waste and set a monthly ceiling.
---

# Cost Audit

An audit that ends with a number and three actions is worth doing. An audit
that ends with a spreadsheet nobody reads is not. The unit here is the cost
per business unit (per product, per app, per customer), because that is the
number that decides whether a product stays alive.

## When to use

- The bill is more than 10% above the previous month.
- A new service, model, database or worker is about to be added.
- Monthly, before the invoice closes: 60% of the month's budget is spent by day 18 in a typical services business.
- Before renewing any annual commitment.

## Step 1 — Get the raw data (10 minutes)

- Pull the last 3 full months of billing data per provider, at daily granularity, with the service, resource and region tags. Three months, not one: a single month hides the seasonal and the one-off.
- Pull the revenue or active-user count for the same period per business unit. Without the denominator, every cost number is meaningless.
- Note one-off items separately: a migration month, a launch spike, a trial of a paid service. They must not be averaged into the baseline.

## Step 2 — Compute cost per business unit (10 minutes)

- For each business unit, allocate direct cost (its own resources, tagged) plus its share of shared cost (network egress, monitoring, the shared database) by the driver that actually scales with it — requests, active users, or rows written. Pick one driver per shared resource and write it down; the allocation is a model, not a truth, and it must be consistent month to month to be comparable.
- Produce two numbers: cost per active user and cost per paying customer. If cost per paying customer exceeds the gross margin of that product, the product is a hobby, not a business.
- Produce the trend: cost per unit over the three months. Rising cost per user with flat revenue is the first sign of a scaling bug, and it is visible here and nowhere else.

## Step 3 — Run the waste queries (15 minutes)

Against the daily cost table, group by service and by month, and look for:

1. **Top 10 line items by month-over-month growth.** The biggest delta is where the money went. Compare this month's top 10 with last month's: a new entry is a change in behaviour, not a rounding error.
2. **Idle and forgotten resources**: unattached disks, stopped-but-not-deleted instances, snapshots older than 30 days, load balancers with no targets, unattached public IPs, staging environments left running 24/7, test databases in production accounts.
3. **Egress and transfer**: usually the biggest surprise on cloud bills. It grows with response size and with chatty clients. Check egress per request: a rising ratio means someone started returning full objects instead of fields, or a poll loop went from 60 minutes to 1.
4. **AI and API spend**: tokens per request, average input and output size, and the fraction of calls that are retries or cache misses. A caching layer that cuts tokens by 70% is usually cheaper than a cheaper model.
5. **Log and observability volume**: retention windows, verbose log levels left on in production, per-host metrics granularity nobody reads. Logs are priced per GB and grow silently.
6. **Idle capacity**: instances with average CPU below 10% or memory below 20% over 30 days, and containers whose memory limit sits far above real usage.
7. **SaaS and per-seat spend**: seats unused in 30 days, duplicate tools doing the same job, an annual plan on a project that is now parked.

## Step 4 — Rank and act (10 minutes)

Rank every finding by **monthly saving / hours of work**:

| Action | Saving | Effort | Verdict |
|---|---|---|---|
| Delete <n> idle resources | $X/mo | 15 min | do now |
| Lower log retention | $X/mo | 30 min | do now |
| Cache the <X> endpoint | $X/mo | 4 h | schedule |
| Move <Y> to a smaller instance | $X/mo | 1 h | schedule |
| Cancel <tool> | $X/mo | 20 min | do now |

Rules:
- Do everything under 30 minutes of work in the same sitting.
- One change per measurement cycle, so the saving can be attributed.
- Never trade away reliability for cost in the same change: if you are
  downsizing, keep the same redundancy and the same alerting.
- A cost reduction that breaks an SLO is a regression, not a saving.

## Step 5 — Set the ceiling (5 minutes)

- Monthly budget per product, derived from its gross margin: infrastructure
  cost should stay under 15% of revenue for a SaaS product, under 30% for
  an App Store product with a store fee, and under 5% for a static
  marketing site.
- Hard ceiling per service and per environment, written down and reviewed
  monthly.
- Alerting: 50% of the monthly budget by day 12, 80% by day 20, and a
  forecast of the month-end total (current spend plus the trailing 7-day
  run rate times the remaining days).
- The rule that matters: if the forecast exceeds the ceiling by more than
  10%, action happens before the invoice, not after.

## What a good answer looks like

```
PERIOD: <month> vs <prior month> | SPEND: $X (+Y% vs prior)
COST PER ACTIVE USER: $A (product 1), $B (product 2)   TREND: +Z%
TOP 3 WASTE: <item> $X/mo | <item> $Y/mo | <item> $Z/mo
ACTIONS: <n> now (<hours>) | <n> scheduled (<dates>)
CEILING: <budget> per product | forecast <$X> -> <under|over> by <%>
VERDICT: <keep|reduce|pause the most expensive product>
```

## Traps

- Cutting the dev environment's cost while it slows the founder's own work:
  the founder's hours are the most expensive line in the company.
- Optimising the largest line item without checking whether that product is
  worth running at all. Sometimes the correct saving is to shut a product
  down, not to make it cheaper.
- Comparing a launch month against a normal month without labelling it.
- Reducing observability to save money, then discovering the outage with no
  data.
- Forgetting the fixed costs: domains, certificates, the CI runner, the
  design tool, the store fees.
