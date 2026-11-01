---
name: incident-triage
description: Use when production is broken and users are affected, to run the first 15 minutes of triage, decide rollback versus fix-forward, and write the incident record while it is still fresh.
---

# Incident Triage

Fifteen minutes of disciplined triage beats an hour of guessing. The goal of
this procedure is to answer three questions, in order: how bad is it, what
changed, and what do we stop right now.

## The first 15 minutes

**Minute 0-3 — Confirm the scope.**
- Is it one product, one endpoint, one region, one customer, or everything?
  Check the error rate and the latency of the affected routes.
- When did it start? Compare the first error timestamp with the last
  successful deploy, config change, DNS change, certificate renewal, or
  third-party status event.
- Who is affected: a segment of users (one plan, one country, one device
  model) or all of them? A segment points at a feature flag or a specific
  code path.
- Write one sentence of scope now: "X% of requests to Y fail since Z". If
  you cannot write that sentence, you do not yet know what is broken.

**Minute 3-7 — Check the four usual suspects.**
- **The last deploy.** 50% of incidents are the last change. Compare the
  error onset with the deploy timestamp. If they are within minutes, treat
  the deploy as the cause until proven otherwise.
- **The dependency.** A third-party API, a payment provider, an email
  service, a DNS resolver, a certificate that expired overnight, a quota or
  rate limit that was hit. Check the provider's status page before
  debugging your own code.
- **The resource.** Disk full, memory exhausted, CPU pinned, a worker pool
  starved, a connection limit reached, a queue backing up. Check the
  resource graph, not just the error log.
- **The data.** A migration that changed a column type, a backfill that is
  still running, a row count that jumped 100x, a cache that now serves a
  stale shape after a deploy.

**Minute 7-11 — Decide: roll back or roll forward.**
- **Roll back** when: the last deploy correlates with the onset, the failure
  is in your code, the previous version is known-good, and the data format
  is unchanged or backward compatible. Rolling back is the fastest path to
  a healthy system and the cheapest decision to defend later.
- **Roll forward** when: the rollback would lose data (a destructive
  migration already applied), the previous version is incompatible with the
  current data, the cause is a third party, or the fix is a one-line config
  change that takes 2 minutes.
- **Do not** roll back a destructive migration without confirming the
  migration's own rollback path works. "Roll back the code but keep the new
  schema" is the most common way to make an incident worse.
- **Mitigate first, root-cause later.** Turning off a feature flag, pausing
  a rollout, failing over a region, or raising a rate limit restores
  service faster than any code change. Mitigation is not the fix; it buys
  the time to do the fix properly.

**Minute 11-15 — Communicate and record.**
- One message, one channel: what is broken, who is affected, what you are
  doing now, and when the next update comes (a real time, within 15
  minutes). No speculation, no apologies without a plan.
- Record in the incident log while you work: timeline with timestamps,
  the scope sentence, each action and its result. The timeline is the
  deliverable of the incident; the root cause is a by-product.

## Rollback triggers (pre-decided, not judgement calls in the moment)

Roll back immediately, without further investigation, when any of these hold:
- Error rate on the core path above 5% for 5 consecutive minutes.
- A payment, sign-in or data-write path is broken for users.
- Data is being written incorrectly or lost.
- The fix requires a code change that takes more than 15 minutes to land.

Do not roll back when the cause is confirmed to be external and unaffected by
your code, or when the rollback destroys data.

## After the service is restored (within 24 hours)

- **Timeline**: what happened, when, in UTC, with the deploy and the alerts.
- **Impact**: affected requests, affected users, duration, estimated revenue impact — only if you can count it; otherwise say "not measured".
- **Root cause**: the specific change that caused it, and the reason the system let it through (missing test, missing alert, missing review, a gap in the rollback plan). "Someone deployed a bad config" is not a root cause; "no staging parity check covered the config schema" is.
- **What made it harder**: missing dashboards, a slow rollback, an unclear owner, a runbook that did not exist.
- **Actions**: at most 5, each with an owner and a date, ordered by prevention before detection before recovery. Every action must be verifiable ("alert on X", "add a test for Y", "document the rollback command in the runbook") — never "be more careful".
- **Blame**: none. Name systems, not people.

## What a good answer looks like

```
SCOPE: 12% of write requests failing since 14:32 UTC (p95 error 500)
CAUSE: deploy <hash> changed the response shape; the parser in <service> rejects it
MITIGATION: rolled back to <hash> at 14:41 -> errors 0.0%
ROLLBACK TRIGGER MET: yes (>5% core path, 5 consecutive minutes)
IMPACT: 380 requests, ~40 users, 9 minutes, revenue not measured
ACTIONS: 1) add contract test for the response shape (owner, date)
         2) alert on write-path error rate >2% for 5 min (owner, date)
NEXT UPDATE: posted 14:45
```

## Traps

- Debugging at the code level while users are affected. Mitigation first.
- Restarting everything to "see if it helps": that destroys the evidence
  and, on a stateful service, the data.
- Editing code directly on the server instead of rolling back to a known-good
  version, which makes the next deploy contain an untracked fix.
- Declaring it fixed when the error rate fell because traffic dropped.
- Writing the root cause as a one-liner and skipping the actions. An
  incident without actions will happen again, usually sooner.
- Waiting for the "perfect" explanation before communicating. The scope
  sentence and the next update time are enough.
