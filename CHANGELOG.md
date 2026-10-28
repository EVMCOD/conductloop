# ConductLoop — Changelog

User-facing changes to ConductLoop (the desktop app and its local server), newest first.

## 2026-10-28 — Billing and license code removed from the repo

ConductLoop is free, so the dead billing/license code is gone.

**What changed**
  - Removed `ops_dashboard/licensing.py`, `ops_dashboard/db_billing.py`,
    `ops_dashboard/web/billing.html`, `docs/BILLING.md` and their tests.
  - Removed the `/license`, `/billing`, `/billing/success`, `/api/license/status`
    and `/api/billing/*` routes (they now 404 like any unknown path). The
    login/signup flow no longer writes a `payment_events` row, and
    `/api/auth/me` no longer returns the founder badge fields.
  - The README no longer lists Stripe as a connector.
  - Database tables are untouched (no destructive migration).

## 2026-10-28 — Evals live con tu clave (aprendizaje L2)

`python -m ops_dashboard.evals run --live` ejecuta los casos de evals contra un
proveedor real con la clave BYOK del llavero (`--key-ref`), solo a mano y nunca
en CI. Presupuesto en EUR y límite de llamadas obligatorios, precio del modelo
obligatorio, informe local comparable con la baseline offline, resultados en
`eval_runs`/`eval_results` (migración 25), `--record` y `--judge` opcionales.
Ver `docs/evals.md`.

## 2026-10-21 — License, billing and Stripe removed from the app

ConductLoop is free; the leftovers of the old license/billing flow are gone.

**What changed** (PRs #69, #72, #73, #74, #75 + this docs PR)
  - No license gate, no `/license` screen (302 → `/`), no
    `/api/license/{trial,activate,checkout}` proxies to the license worker;
    `OPS_LICENSE_ENFORCE` no longer set by the desktop sidecar.
  - No Stripe in the app: checkout/cancel/webhook endpoints return 410,
    purchase emails removed, `/api/pricing-tier` removed, `/billing` is a static
    free-edition page (founders keep their badge, no price).
  - All price strings removed from i18n; the "edition" onboarding step removed
    (checklist is 9 steps).
  - Live license-worker tests and `scripts/test-fleet` removed (they created
    trials in the production D1); bare `pytest` never runs live tests.
  - `conductloop.egg-info/` untracked.

**User impact**: none for ≥1.1.0. Existing license/trial keys stay on disk and
are ignored. Desktop 1.0.x keeps validating against the worker, which now
accepts any existing key (EVMCOD/conductloop-license #1).

## 2026-10-21 — Updater signing key fixed (reinstall needed once)

**Problem**: v1.1.x/v1.2.0 embed updater pubkey `D68774E13CB63DED`, but
releases are signed with `770EA4C292DC6BD2` (the only updater key available),
so those installs can never auto-update.

**What changed**
  - `desktop/src-tauri/tauri.conf.json`: updater pubkey is now
    `770EA4C292DC6BD2` (verified against the published v1.2.0 signature).
  - `desktop/UPDATER_KEY_ID` + `scripts/check_updater_pubkey.py`: CI and
    `scripts/release-desktop.sh` fail on any key id mismatch (before building,
    and again on the produced `.sig`).
  - `scripts/release-desktop.sh`: removed the unused hard-coded pubkey and the
    stale "7-day free trial, then $10/month" release-note copy.

**User impact**: anyone on v1.2.0 or earlier must reinstall the DMG **once**
(unless the `D68774E13CB63DED` private key turns up; v1.1.0 embeds yet another
key, `CADDC7E4879ACD78`). Later releases update
automatically. See `desktop/docs/DISTRIBUTION.md`.

## 2026-07-16 — ConductLoop goes FREE (no subscription, no payment)

**Strategic pivot**: ConductLoop is now 100% free for everyone. The $10/mo
subscription and the Lifetime tier were retired on 2026-07-16. All features
unlocked for every install.

**What changed**
  - `ops_dashboard/licensing.py`: `enforce()` now always returns False.
    `status()` returns `{licensed: true, plan: "free", edition: "free"}`.
    Functionality kept for future cloud-only features (cross-device sync,
    AI hosting, team workspaces) — those will reintroduce paid tiers behind
    per-feature gates, NOT a global subscription.
  - `ops_dashboard/server.py`:
    - `_billing_checkout_subscription` returns a friendly "free edition" JSON
      instead of creating a Stripe session (kept for old client compatibility).
    - `_billing_checkout_lifetime` ditto.
    - The license gate on `/` (which used to redirect to `/license` when not
      licensed) is now always open because `enforce()` is False.
  - `ops_dashboard/web/license.html`: rewritten as a Free Edition landing card.
  - `ops_dashboard/web/billing.html`: shows a single "FREE EDITION" card.
    The legacy pricing/auth JS is preserved below the cut, ready to resurrect
    when cloud features ship.
  - `ops_dashboard/web/billing-success.html`: stops polling for a Stripe key;
    now displays a generic "Welcome, free edition" message and an optional
    best-effort fetch of any legacy session-bound key.
  - `README.md`: updated License section to reflect free-for-everyone.

**What did NOT change (intentional)**
  - Stripe / Apple IAP code in `conductloop-license` Cloudflare Worker is
    kept intact but dormant. It will be re-pointed at per-feature checkout
    when cloud-only features land.
  - License keys that were issued before today still validate against the
    worker (so anyone who paid during beta keeps their install recognized).
    New installs don't need a key.
  - The `/api/license/trial` and `/api/license/activate` endpoints still
    work — useful if you want a key for tracking, but not required.

**Migration impact**
  - The daily license-expiry cron that used to email the chairman 7 days
    before a license expired is now a no-op (no new trials expire).
  - The Boardroom no longer auto-reminds the chairman about an upcoming
    license renewal.

This is the biggest pricing change ConductLoop has had. From here on, growth
comes from usage, virality, and (later) optional cloud upgrades — not from
a paywall.

---
