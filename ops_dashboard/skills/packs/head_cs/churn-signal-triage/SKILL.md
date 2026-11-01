---
name: "churn-signal-triage"
description: "Use when churn is rising or usage is falling, to run the weekly signal check, match each signal to one intervention, and tell a bad month apart from a real churn pattern."
---

# Churn Signal Triage

Read the leading indicators weekly, act on the account with the clearest signal first, and separate one bad month from a pattern before
changing the product or the pricing.

## When to use

- Logo or revenue churn moved more than 1 percentage point in a month.
- Usage, engagement, or seat count is trending down across accounts.
- A renewal is inside 60 days and the account is not healthy.
- The founder asks "are we losing customers" and nobody has the numbers.

Not for: a support escalation on a single account (handle it as a ticket), and not for pricing research (that is a discovery or pricing
procedure).

## Step 1 - Fix the definitions before looking at the numbers

Churn is measured four ways, and mixing them makes every conversation vague. Write down which one is in use:

- **Logo churn**: accounts that cancelled in the period, over accounts active at the start of the period.
- **Revenue churn**: recurring revenue lost, over recurring revenue at the start of the period.
- **Gross revenue retention**: revenue retained including downgrades and contractions, before any new business. This is the number that reflects the existing base.
- **Net revenue retention**: gross revenue retention plus expansion and minus churn.

Rules: a free-tier cancellation is not customer churn for a paid product; count it separately. A plan downgrade is churn in logo terms but
contraction in revenue terms. State the definition in the same sentence as the number, every time.

## Step 2 - The signals that predict churn

Watch these weekly, per account. Each has a threshold so it is a fact, not an impression:

| Signal | Threshold | Why it matters |
|---|---|---|
| Usage collapse | weekly active use drops 50% or more versus the account's own 4-week average | the habit is broken |
| Usage decay | weekly active use falls for 3 consecutive weeks | slow drift toward the exit |
| Login decay | the account logs in but stops doing the core action | passive account, no value |
| Support escalation | 2 or more tickets in 14 days, or a ticket mentioning cancel, competitor, refund or "not working" | friction is surfacing |
| Champion departure | the power user or admin leaves the account | the internal sponsor is gone |
| Seat or usage contraction | seats or volume down 20% month over month | the business is shrinking |
| Billing failure | a failed payment, a downgrade request, or a downgrade to a lower plan | intent to leave or to shrink |
| NPS detractor | score of 0 to 6, with a written reason | named, specific dissatisfaction |
| Renewal proximity | renewal inside 60 days and the account is not in the healthy band | churn is imminent, not hypothetical |
| NPS silence | survey response rate below 15% for a segment | missing data; the risk is unmeasured |

Use the account's own history as the baseline, not a global average. A seasonal customer with 10% usage in December is not a churn signal.

## Step 3 - The weekly check (30 minutes)

1. **Pull the list**: every account with any signal above, for the last 7 days.
2. **Rank by value and urgency**: sort by revenue at risk, then by renewal date. Accounts inside 60 days of renewal come first regardless of
   size, because the decision is imminent.
3. **Assign one owner per account.** An unowned signal is a warning nobody acts on.
4. **Apply exactly one intervention per account per week.** Two actions at once makes it impossible to learn which worked.
5. **Record the outcome**: what was done, the date, and the result 7 days later.

Output is one page: accounts contacted, accounts recovered, accounts lost, and the signal mix.

## Step 4 - Match the signal to the intervention

- **Usage collapse**: human outreach within 48 hours, ask what changed, not why they left. Offer a re-onboarding on the one workflow they
  stopped using. No discount in the first contact.
- **Usage decay**: a targeted tip or template for the specific feature they stopped using, plus one question about what they are doing now.
- **Support escalation**: the support ticket owner and the account owner talk before replying. Fix or commit to a dated fix first, then
  write to the customer. A reply that promises a fix with no date is a churn signal of its own.
- **Champion departure**: reach the account within 48 hours, ask what changed, and rebuild the internal sponsor. Often a process or an
  ownership question, not a product one.
- **Contraction**: ask whether the reduced volume is permanent or seasonal, and offer the right-sized plan. A right-sized plan protects
  revenue better than a retention discount.
- **Billing failure**: recovery workflow, dunning, and a human touch after the second failed attempt.
- **NPS detractor**: reply to the written reason within 5 business days, with a specific action or a specific "no, and why".
- **Renewal proximity on an unhealthy account**: do not wait for the renewal date. Start 60 days out, with a value recap based on the
  account's own usage numbers.

## Step 5 - Tell a bad month from a real pattern

A bad month has a cause you can name and it reverses. A real pattern keeps repeating after the cause is gone. Five tests, all on the last 6
months of data:

1. **Persistence**: the signal is present in at least 3 of the last 6 months, in at least 3 accounts each month.
2. **Cohort shape**: the churn is concentrated in one signup cohort or one plan, rather than spread across the base.
3. **Funnel shape**: a specific step before activation shows a falling conversion across cohorts.
4. **Response**: the same intervention was tried and recovery stayed under 15%.
5. **Segment shape**: one segment (self-serve, one platform, one country, one price point) is leaving while the rest is flat.

Decision rule: one test passing means investigate and act on the accounts. Two or more tests passing means a product or pricing change goes
into the next roadmap cycle, with the churn metric as the acceptance criterion. Zero tests passing means the month was bad: fix the specific
cause, keep the routine, and do not change the product.

Always check for a cause before concluding a pattern: an outage, a broken release, a price change, a competitor launch, a billing migration,
a holiday period, or a change in how churn is measured. A measurement change looks exactly like a churn spike.

## What a good answer looks like

- The four retention definitions, and which one the numbers use.
- Logo churn, revenue churn and gross revenue retention, with the 6-month trend.
- The signal table with thresholds and the list of accounts triggering each one this week.
- Every account owned, with one intervention and a date.
- The 5 pattern tests, scored pass or fail, and the verdict: bad month, investigate, or product change.
- The measurement check: did anything change in how churn is counted or in the billing flow?

## Common traps

- Reporting revenue churn when the question was about lost accounts, and arguing about the number instead of the accounts.
- Using a global benchmark instead of the account's own history, so seasonal accounts look like churn.
- Chasing every signal at once instead of ranking by value and renewal date.
- Sending a discount as the first move on a usage collapse, which teaches customers that usage decline is how discounts start.
- Declaring a trend from one bad month, or a pattern from two.
- Failing to check whether the churn spike coincides with a release, an outage or a change in how the metric is computed.
- Treating NPS silence as satisfaction.
