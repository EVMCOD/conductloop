---
name: "unit-economics-review"
description: "Use when checking whether a product's unit economics are healthy before spending on growth, pricing or a funding decision."
---

# Unit Economics Review

A monthly check of whether one product makes money per customer. Ends with
go-to-scale, fix-first, or stop, and the single weakest number.

## When to use

- Monthly or quarterly close on a product with paying customers.
- Before spending on ads, raising prices, or investing in a sales push.
- Before a funding conversation: investors check these numbers first.
- When a product is growing but the founder's income is not.

Not for: pre-revenue products (there are no unit economics yet; use
opportunity assessment), or a portfolio-level cash question (use runway
planning).

## Step 1 - Fix the definitions, then the numbers

Define once and write them at the top of the review:

- ARPA = MRR / paying customers. Not revenue including one-off fees.
- Gross margin % = (revenue - cost of delivery) / revenue, where cost of
  delivery is hosting, third-party API calls, payment fees, app store cut,
  and support time valued at a stated hourly rate.
- Churn = customers lost in the period / customers at the start of the
  period. Logo churn, not revenue churn; report both when they differ.
- CAC = all acquisition spend in the period / new paying customers. Include
  founder time spent selling if it exceeds 2 hours a week.
- LTV = ARPA x gross margin % / monthly churn rate. With monthly churn of
  0.05, LTV = ARPA x margin% x 20.

If churn is zero (a young product), LTV is not computable. Write "LTV not
yet measurable" and use a bounded scenario instead of inventing a rate.

## Step 2 - Compute the ratios

- CAC payback (months) = CAC / (ARPA x gross margin %).
- LTV to CAC = LTV / CAC.
- Revenue per founder hour, for a solo founder: MRR divided by the hours
  spent on that product per month, valued at a stated rate. It is how a
  small product is compared against a bigger one.

## Step 3 - Apply the thresholds

| Metric | Healthy | Watch | Broken |
|---|---|---|---|
| Gross margin | 70%+ | 50-70% | under 50% |
| LTV / CAC | 3x+ | 1-3x | under 1x |
| CAC payback | under 6 months | 6-12 | over 12 |
| Monthly logo churn | under 3% | 3-5% | over 5% |
| Monthly revenue growth | 5%+ | 0-5% | negative |

The rules that come with them:

- Gross margin under 50%: the product cannot scale profitably at any
  acquisition rate. Fix delivery cost or price before spending on growth.
- LTV/CAC under 1: every euro acquired loses money. Stop acquisition work
  even if revenue is growing.
- CAC payback over 12 months: the money returns too late for a small
  business. Cut CAC or lift ARPA.
- Churn over 5% a month: LTV collapses no matter how good CAC looks. Fix
  churn first, because acquisition spend compounds the leak.
- Under 20 paying customers: the ratios are noise. Report raw counts and
  the trend, not the ratios, and set the review for when 20 customers
  exist.

## Step 4 - Separate the leaks

Growth with bad ratios means money is pouring into a bucket with a hole.
Classify every dollar of spend by the metric it moves: spend that increases
ARPA (upgrades, onboarding), spend that decreases churn (onboarding,
support, reliability), or spend that acquires customers (ads, outbound,
directories).

If more than 60% of acquisition spend goes to the third bucket while churn is
above 5%, move that spend to the second bucket first: fixing churn is worth
about 5x its cost in avoided acquisition spend, since each churned customer
must be re-acquired at full CAC.

## Step 5 - Write the verdict

- SCALE: all thresholds healthy. Recommend a specific increase in spend (a
  named percentage) and the metric to watch weekly.
- FIX FIRST: one named ratio is broken. Recommend the single change and the
  expected direction of that ratio in 8 weeks.
- STOP: gross margin under 50% and LTV/CAC under 1, or churn above 10% with
  no path down. Recommend archiving and moving the surface area to a better
  product.

## What a good answer looks like

One page: the definitions used and the period covered; a table of raw inputs
(MRR, customers, ARPA, spend, churn) each marked measured or ASSUMPTION; the
ratios against the thresholds above; the leak classification; the verdict in
the first line with one action and one metric to watch; and a note on what
the numbers cannot tell you yet.

## Common traps

- Using revenue churn where logo churn is meant, or mixing both in one
  formula.
- Excluding founder time from cost of delivery, which flatters margin.
- Counting one-off setup fees in ARPA, or computing LTV with an annual
  churn rate against a monthly ARPA.
- Treating a launch spike as growth: measure month 3 against month 1.
- Averaging across products to hide one broken unit; each product has its
  own economics.
- Reporting ratios for a product with 5 customers as if they meant anything.
