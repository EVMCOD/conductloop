---
name: deploy-verification
description: Use when a deploy has just finished, to run a fixed post-deploy checklist against the live URL and decide ship, monitor or roll back from evidence instead of hope.
---

# Deploy Verification

A deploy is not done when the command returns. It is done when the live URL
has been checked by someone who did not build it. This checklist takes 10
minutes and catches the failures that a green build cannot.

## When to use

- After every production deploy, without exception.
- After a migration, a config change, a DNS change, or a secret rotation.
- Before telling anyone the release is out.

## Run it in this order (10 minutes)

1. **Version check (2 min).** The live response must carry the new version
   (a commit hash, a build id or a release marker in the footer, an API
   header, whatever your stack uses). If the live version is not the version
   you just built, the deploy did not happen where you thought it did — stop
   here and find out why before touching anything else.
2. **Health endpoint (2 min).** Request the health endpoint and the readiness
   endpoint. Expected: HTTP 200, and a body that reports the new version and
   shows its dependencies as up. A health endpoint that always returns 200
   is worthless; check that it actually inspects a dependency (database,
   cache) and that a broken dependency makes it fail.
3. **One real user path (3 min).** Execute the single most valuable path in
   production, as a user: sign in, create the thing, and read it back on a
   second request. Not a health check — the actual flow, with real data in
   the database. For a payment flow, use the smallest real amount and refund
   it. If the app writes to the database, confirm the row is really there
   with a direct query, not just that the UI showed a success toast.
4. **Logs for errors (2 min).** Read the last 10 minutes of application and
   edge logs. Expected: no stack traces, no 4xx/5xx spikes, no repeated
   warnings from the new code path. Look for the version tag so old-version
   noise does not hide a new error. Check the count, not just the presence:
   one occurrence of a real error is a defect.
5. **Rollback trigger check (1 min).** Confirm the rollback path still
   exists and is one command or one click: the previous image is tagged, the
   previous migration is reversible, the previous config is retrievable. If
   you cannot describe the rollback in one sentence, the release is not
   safe to continue.

## Additional checks by change type

| Change | Extra check |
|---|---|
| Database migration | Row counts before/after for the touched table; no table locked; no unexpected full-table scan |
| Dependency or lockfile change | Full build + full test suite, not a cached CI run |
| Environment variable or secret | The new value is actually read (log a presence flag, never the value) |
| Feature flag | Flag defaults correctly for users without the flag set |
| Static assets / CDN | Cache headers, the new asset hash is served, old assets are purged |
| Worker / edge function | Cold start time measured, not assumed |
| Certificate, domain, DNS | TTL lowered before the change; propagation confirmed from outside |

## Decision

- **Ship** — all five checks pass. Post the version and the check result in
  the release log.
- **Monitor** — the deploy is fine but one signal is ambiguous (for example
  elevated latency with no errors). Set a 30-minute re-check with a named
  threshold that would change the verdict.
- **Roll back** — any of: the live version is not the new one, the user path
  fails, 5xx rate over the new endpoints exceeds 1% of requests, or the
  database state is wrong. Rolling back a bad release is cheap and
  reversible; debugging in production while users are broken is not.
- **Forward fix** — only when the rollback is itself risky (a destructive
  migration already applied, a data change that cannot be undone). Then
  write down why forward was the only option, before doing it.

## What a good answer looks like

```
VERSION: live <hash> == deployed <hash> | HEALTH: 200 (db ok, cache ok)
PATH: sign-in -> create project -> read back in a new session: PASS
LOGS (10 min): 0 errors, 0 new warnings | 5xx: 0.0% of 412 requests
ROLLBACK: one command, previous image tagged, migration reversible
VERDICT: ship | monitor | roll back
```

## Traps

- Verifying in a browser with a cached service worker or a stale CDN edge.
  Check an incognito window and, when in doubt, a different network.
- Trusting a green CI badge instead of the live version string.
- Verifying the landing page and calling it done; the broken path is always
  the one that writes.
- Testing with an admin account; permission branches are separate code.
- Rolling back without checking whether a migration already ran against the
  old schema, and rolling back code onto a schema it cannot read.
- Deploying and immediately starting the next task: the 10 minutes here is
  cheaper than the hour of debugging later.
