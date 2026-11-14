---
name: mobile-release-safety
description: Use when a mobile build is about to roll out to users, or when a live release is misbehaving, to stage the rollout, watch the crash-rate threshold and keep a rollback ready.
---

# Mobile Release Safety

A mobile release cannot be rolled back by deploying a fix. The binary is on
the user's device. Everything about this procedure exists because that fact
changes the calculus: staged exposure, remote kill switches, and a decision
rule agreed before the release, not during the incident.

## Before the release (the night before)

- **Version discipline.** The build number increments and is unique. A build
  that was uploaded and rejected can be reused only after it is approved.
- **Feature flags default to off** for anything risky in this release. This is
  the actual rollback mechanism for a native app: a remote config flip, not a
  new binary.
- **Minimum supported version.** If the release changes a data format or an
  API contract, decide now whether old clients are supported. If they are not,
  enforce the minimum version server-side with a message and a link to update,
  never with a crash.
- **Backend compatibility first.** The API ships before the app that needs it,
  and the old API version keeps working for at least the longest review
  cycle of the currently live app (assume 2 weeks minimum).
- **Migration is reversible.** Database changes ship as add-then-backfill-
  then-switch, never as a destructive edit in the same release.
- **Monitoring exists before the rollout**: crash-free rate, ANR rate,
  the key business event, and a version breakdown. If you cannot see the crash
  rate per version, you cannot run a staged rollout.

## The rollout ladder

| Stage | Exposure | Minimum soak | Gate to advance |
|---|---|---|---|
| 0 | Internal / TestFlight / internal Play track | 1 hour | zero crashes on the new build |
| 1 | 1% (Play) / phased release 1% (iOS) | 24 hours | crash-free >= 99.8% |
| 2 | 10% | 24 hours | crash-free >= 99.8%, no ANR spike |
| 3 | 50% | 48 hours | crash-free >= 99.8%, support volume flat |
| 4 | 100% | 48 hours | then promote to the "release" track |

Soak time is the point. A crash that only affects 1% of users shows up in
1% of the samples, and 1 hour of 1% is a handful of devices. The percentages
exist to bound the damage, not to prove correctness.

## The crash-rate threshold that halts the rollout

- **Halt immediately**: crash-free users below 99.5% on the new version, or
  any single crash signature above 0.2% of sessions on the new version.
- **Halt and investigate**: ANR rate above 1% of sessions, or a business
  event rate (sign-up, checkout start) more than 20% below the previous
  version's rate.
- **Watch**: crash-free between 99.5% and 99.8%, or a support ticket count
  above 2x the daily average.
- Baseline to compare against is always the previous version on the same
  cohort, not an absolute number and not the app's all-time average.

## The first hour after each stage

1. **5 minutes**: open the crash dashboard filtered by the new version. Look
   at crash-free rate, top crash signatures, and the count of affected
   sessions.
2. **15 minutes**: check the key business event for the new version against
   the previous version. A crash-free rate can be perfect while every new
   user is stuck on a blank screen.
3. **30 minutes**: read the top 5 stack traces end to end. If any single
   stack trace explains more than a third of new-version crashes, roll back
   now and diagnose offline.
4. **45 minutes**: check the release-specific dashboard for network errors
   and API error rates. A native release often breaks on the server side
   (a renamed field, a stricter validation) while the app itself is fine.
5. **60 minutes**: check support tickets and any automated replies. A
   complaints pattern in under an hour is a signal even when the numbers are
   still green.

## Rolling back

Order of preference:

1. **Remote flag off** — seconds, and the reason this is the primary
   mechanism. Every risky behaviour ships behind a flag.
2. **Halt the staged rollout** — hours to a day, and the app on device keeps
   working because the old version is still served.
3. **Ship a hotfix** — days, subject to review. A platform review can take
   24-72 hours, so never design a release that depends on a hotfix.
4. **Kill the backend feature** — only if the server can degrade gracefully
   for old clients.

Never: force-uninstall the app from users' devices, or make users reinstall.
Never: release a version you cannot stop, with no flag and no stage gate.

## What a good answer looks like

```
RELEASE: 2.3.0 (142)   RAMP: 10% -> 50%
CRASH-FREE (new ver): 99.86%   baseline (prev): 99.91%   threshold: 99.50%
TOP SIGNATURE: <name> - <n> sessions (0.04%) - first seen <when>
KEY EVENT: signup <x>% vs <y>% baseline   ANR: 0.3%
FLAGS DISABLED: <list>
DECISION: advance to 50% | hold | halt
NEXT CHECK: <time>
```

## Traps

- Treating "no crash reports yet" as health. Most users do not report
  crashes, and a silent failure looks exactly like no crash reports.
- Rolling out to 100% before the first stage has soaked overnight.
- Comparing the new version's crash rate against the app's lifetime average.
- Releasing a server-dependent feature on the same day, so the rollback
  decision is ambiguous about the cause.
- Forgetting old clients: a schema or API change breaks the 2.3.1 install
  that is still running, and you cannot patch it fast.
