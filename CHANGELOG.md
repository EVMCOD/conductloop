# ConductLoop — Changelog

User-facing changes to ConductLoop (the desktop app and its local server), newest first.

## 1.2.3 — 2026-10-29 — A home for your notes and a Brain that helps without a key

The first version that installs from the app: in 1.2.2, "Check for Updates…"
(or the quiet check a little after start-up) offers it. On the Mac, what
changes is the first part of the list below (the notes home, the Brain without
a key, Setup); the Linux and Windows items are groundwork for those systems and
leave the Mac app as it was.

- **A home for your notes.** Without projects (a fresh install has none), the
  dashboard opened on an empty project canvas and search hid behind ⌘P. It now
  opens on "Your notes": how many are indexed, a box to ask a question (it goes
  to the Brain in Chat, filled in, not sent), a search box and the recent notes,
  each opening in the note view. With projects the canvas is unchanged.
- **The Brain is useful without an AI key.** With no provider it answered with a
  placeholder, even when it had found the right note. It now quotes the lines of
  the notes it found that share the most words with the question, with their
  [N] sources, and says how to get written answers. Same in Chat, team mode
  included.
- **Setup marks what is optional.** Semantic search, projects and the memory
  export to Obsidian are extras: shown with "–" and "optional", and no longer
  counted against "ready". Their hints no longer ask for a terminal (`pip
  install`, `OPS_PROJECTS_FILE`, `ops obsidian-sync`), which the desktop app does
  not have, and the checklist shows folder names as text.
- The dashboard's "Aprendizaje" link and "Yo creo — segundo cerebro" node are in
  English, like the rest of the dashboard.
- **Windows groundwork: four fixes that cannot be tested on a real Windows machine yet.** The check that the desktop app is still running no longer ends the app on Windows (it used a signal that Windows treats as "terminate"); the local server refuses to start when its port is already taken, instead of sharing it with another program; the app token and server nonce are generated on Windows as well; and setting private file permissions is a documented no-op there instead of an error. macOS and Linux behave exactly as before. Windows itself is still not supported: there is no installer and no secure key storage yet.
- Linux: `scripts/linux_update_manifest.py` builds and checks the update manifest for the AppImage (the same format as the macOS one, `linux-x86_64-appimage` only). It signs nothing and publishes nothing, and the Linux app still does not check for updates; this is only the groundwork for it. macOS is unchanged.
- **Linux: secrets go to the system keychain (Secret Service).** On Linux, API keys and the other stored secrets now use the desktop keyring (GNOME Keyring, KWallet or KeePassXC), under the same names as on macOS. If there is no keyring (no D-Bus session, a headless machine, a locked keyring nobody unlocks), ConductLoop says so on the platform status, keeps provider keys in the encrypted vault and refuses connector tokens; it never stores them in plain text without telling you. If the keyring exists but cannot be read, ConductLoop stops with an error instead of creating a new key that would make saved connection credentials unreadable. macOS is unchanged. Experimental: not tested on a real Linux desktop yet.
- Linux: a CI workflow, "Linux bundles", builds the AppImage and the .deb on every pull request that touches the desktop app and keeps them for 3 days as downloadable artifacts. It builds on `ubuntu-22.04` instead of `ubuntu-latest` so the AppImage works with an older glibc; it is the only job pinned to a runner image. CI only: the app does not change.
- **Linux: a script to build an AppImage and a .deb.** `scripts/build-linux.sh` freezes the server with PyInstaller and runs `tauri build --bundles appimage,deb`; the Linux settings live in `desktop/src-tauri/tauri.linux.conf.json`, so the macOS build does not change. No signing and no updates on Linux (the app does not look for updates at start-up there). Experimental: the packages have not been installed or run on a Linux desktop yet.
- **Linux: a File menu with Close Tab (Ctrl+W) and Close Window (Ctrl+Shift+W).** The default Linux menu has no File menu, so the two shortcuts did nothing there. The macOS menu does not change. Not tested on a real Linux desktop yet.
- **Linux: the command sandbox no longer lets commands reach local sockets.** Tests and linters run by agent sessions on Linux could still connect to the desktop's D-Bus session bus, the X11 server, ssh-agent/gpg-agent and the Docker socket from inside bubblewrap; those are now hidden. Without bubblewrap, commands stay off. macOS is unchanged. Sockets nested deeper than three levels under /tmp or /run, or in other directories, can still be reached (a seccomp filter would close that).
- **Honest about what each system can do.** The in-app browser is now off on Linux and Windows (it only exists on macOS 14+), and the Browser page says why instead of offering to enable it. On Windows, code sessions are turned off with an explanation: there is no OS sandbox there yet and ConductLoop never runs commands without one. Where the sandbox is missing on Linux, the Sessions page says that commands are off. macOS is unchanged.
- Docs only: `docs/PORTABILITY.md` audits everything that is macOS-only (keychain, sandbox, in-app browser, paths, sidecar, menu, updater, CI) and plans the Linux (experimental) and Windows work. Nothing changes in the app.

## 1.2.2 — 2026-10-29 — Updates that work, a signed DMG and the folder picker in Setup

**Install this one from the DMG** (1.2.1 cannot update itself, see below). From
1.2.2 on, new versions install from the app.

- **The app can update itself.** "Check for Updates…" is in the ConductLoop menu,
  and the app also looks once, quietly, a little after it starts. When there is a
  new version it asks; "Install and restart" downloads it, checks its signature,
  replaces the app and restarts it. Nothing is installed without that click. Up to
  1.2.1 nothing ever started a check, so no install could update itself: from
  1.2.1, install this version from the DMG once more.

- **The folder picker in Setup too.** Setup → Library now has "Choose…" next to
  each folder field and "Add folder…" for the folders to index, like the
  welcome page. It only fills the field: "Save folders" saves.

## 1.2.1 — 2026-10-29 — Backups, a folder picker, a browser that stays in its page, and an app that starts

The first release since 1.2.0 (2026-07-23), so it also carries every dated
section below down to the updater key fix. **If you are on 1.2.0 or earlier,
install this one from the DMG**: those builds embed an updater key that does
not match the releases and can never update themselves (see "Updater signing
key fixed" below). From 1.2.1 on, updates install from the app.

- **Fixed: the in-app browser got in the way of the rest of the app.** Leaving
  the browser page left the open web page drawn over the dashboard and every
  other page; coming back with Go › Back showed no page at all. ⌘W quit the
  whole app instead of closing the tab, and ⌘[ / ⌘] left the browser instead
  of going back in the tab. Now the tabs are hidden as soon as you leave the
  browser page and shown when you come back, ⌘W closes the tab (Close Window
  is ⇧⌘W) and Back/Forward act on the tab. Also: the URL bar shows the page you
  are on, new tabs are called "New tab" instead of "… about:blank", messages
  clear themselves, an address that points to your own machine or your LAN
  says it was blocked instead of doing nothing, the first click on the app's
  bar after using a page is no longer lost, the agent panel starts from your
  configured AI provider, and "Ask" without a key points to Credentials.

- **Fixed: the desktop app never got past "Connecting to local server…".** The
  window waits for its own server by looking for a per-launch id in
  `/api/health`, but the server only echoed the first 16 characters of it. The
  window never recognised its server, gave up after about two and a half
  minutes and loaded the dashboard without the start-up nonce, so every page
  answered 403 `app_token_required`. The id is now echoed in full.

- **Fixed: the Setup and Boardroom pages did nothing.** A script error stopped
  every script on both pages. Setup (the checklist, the library folders and
  "Index now") stayed on "Checking…". Boardroom never listed the C-suite: a
  truncated function and a missing `escapeHtml` broke it, and it linked a
  stylesheet that does not exist. Both work again. The "Ideas that keep coming
  back" list in Setup, which was drawn from the wrong data, now shows the themes.
  A new test runs every page script through `node --check`, so this cannot ship
  unnoticed again.

- **Backups you can trust, and restore from the app.** Setup → Backups takes a
  backup with one click, lists the last ones and restores any of them. The app
  also takes one on its own once a day, and one right before an update changes
  the database (migration 26 deletes tables). Backups used to be a copy of the
  database file: one taken while the app was writing could miss the latest
  changes or come out damaged, and inside the installed app it looked for the
  database in the wrong folder and failed. They now go through SQLite's backup
  API, are checked before they are kept, and live in `backups/` next to the
  database (the last 30). A restore first backs up the current state, refuses a
  damaged file or one made by a newer version, and brings an older backup up to
  date. From the terminal: `python -m ops_dashboard backup`, `backups` and
  `restore <name>`.

- **Content Security Policy for the desktop app.** The Tauri window loads the UI
  from the local server, so the server's header is the policy that applies to
  it; it now restricts everything to the app itself (`default-src 'self'`, no
  remote origins, no `eval`, no frames) plus Tauri's IPC. `'unsafe-inline'`
  stays only for scripts and styles because the older pages still contain inline
  code (a test now stops it from growing). The loading page gets a strict policy
  in `tauri.conf.json` (it was `null`). Images from other sites inside notes no
  longer load. See `docs/CSP.md`.

- **Pick your folders instead of typing them.** In the desktop app, the
  folder fields of the welcome page have a "Choose…" button that opens the
  macOS folder picker. It only fills the field; nothing is saved until you
  click Save. In a browser there is no button and the path is typed as before.

- **A first-run welcome page, and a notes folder you can choose.** On a fresh
  install the app now opens a welcome page that asks for two things, both
  optional: the folder with your notes (it reads it, never writes into it) and
  the folder where ConductLoop keeps its own notes, and an AI key. The key goes
  to the Keychain (or the encrypted file when there is none) and is never shown
  again, logged or sent back. The choice of folder is saved in the app, so it no
  longer needs the `OPS_VAULT_ROOT` environment variable, which the installed
  app had no way to set; it can also be changed later in Setup → Library. The
  page is shown once, and never to anyone who already has notes indexed, a key
  or any setting. The old "quick tour" is still at `/tour`.

- Contributor tooling: `scripts/verify.sh` (also `make verify`) runs the same checks as CI locally, with `--fast`, `--offline` and `--strict` options and an optional pre-push hook (`scripts/install-hooks.sh`). The mypy typed-module list now lives in `scripts/mypy-ratchet.txt`, shared by CI and the script.

- Contributor tooling: added a pull request template (with the local-verification, migration, changelog, no-personal-data and docs checklist) and GitHub issue forms for bug reports and feature requests; blank issues are off and vulnerabilities point to private reporting.

- Release tooling: `scripts/release_check.py --version X.Y.Z` is a dry-run pre-flight for desktop releases (versions in sync, dated CHANGELOG section and notes, updater key id, minimum macOS, updater endpoint, clean git tree). It publishes nothing. See `docs/RELEASING.md`.

## 2026-10-28 — Leftover billing tables and Stripe integrations removed

> **⚠️ This deletes data from your local database.** Migration 26 drops the
> tables `entitlements`, `founders` and `payment_events` and every row in them
> (old plan grants, founder badges, payment audit rows). Nothing in the app reads
> them any more. If you want to keep a copy, back up `ops.db` before updating
> (the data folder is `~/Library/Application Support/com.conductloop.desktop/data`
> on the desktop app). It does not touch any other table, and it is safe to run
> more than once.

**What changed**
  - New migration 26: `DROP TABLE IF EXISTS` for those three tables (their
    indexes go with them). New databases never create them.
  - Removed the entitlement helpers (`get_entitlement`, `grant_entitlement`,
    `list_entitlements`, `revoke_entitlement`) from `db_auth`/`db`. Signup no
    longer grants a "free" plan, and `/api/auth/me` no longer returns an
    `entitlement` field.
  - Removed the generic Stripe connector (`connectors.py`: tester, tools and
    catalog entry) and the Stripe source of the revenue indexer. The Apple App
    Store and Google Play sources and the mock source are unchanged.
    Connections or revenue sources of type `stripe` that you saved earlier stay
    in your database but can no longer be tested or polled.
  - Removed the `cloudflare-worker/` directory (an unused status-page Worker);
    the local `/status` page is unaffected.
  - Removed the founders mock data from the demo build and the leftover
    references in docs, i18n and tests.

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

## 2026-10-25 — The vault no longer hangs the app

**What changed** (PRs #30, #31)
  - A note folder that stops answering (an unmounted drive, a stalled sync
    folder) can no longer freeze the app: vault reads have a deadline, and the
    API answers `503 vault_unresponsive` instead of hanging (deadline 240 s,
    `CONDUCTLOOP_VAULT_TIMEOUT`).
  - A new list of "things you linked and never wrote": `[[links]]` in your
    notes that point to a note that does not exist.

## 2026-10-24 — Offline search, credentials screen and the vault of pages

**What changed** (PRs #15–#29)
  - **Offline semantic search** (#21): the desktop app embeds notes with an
    ONNX engine (fastembed), with no network, key or credit. The app grows from
    41 MB to 88 MB; the model downloads once, on first use, into the data
    folder. Changing the model re-embeds everything; the configured provider wins
    over whatever happens to be installed.
  - **Credentials** (#25): every key of the app lives in the macOS keychain, or
    in a passphrase-encrypted file if there is none — never in plain text, and
    values are never shown again. `/credentials` lists them all; keys already in
    plain text are moved with one button. The setup checklist now points there.
  - **Servers panel** (#24): `/servers` shows, per capability (chat, image,
    video, speech, search), which server will answer and which others could.
  - **The map of your work** (#15–#20, #22, #23): the library draws a map of
    your projects from your notes and writes it into the vault as pages
    (`pages/Index.md`, `pages/<project>.md`, `pages/areas/<area>.md`). It only
    changes when something changes, detects lines missing from your hand-written
    index and proposes them, inserts them without touching your text, and
    keeps an "unclassified" section and a list of forgotten notes. A **daily
    note** gets a marked block with what happened in the vault, without
    overwriting what you wrote. The agent's own instructions point at the map.
  - **Where each project stands** (#26–#29): each project page shows "Where this
    stands" (from your handoff notes) and "What remains open", plus a
    `pages/Waiting.md` page of what is waiting for you, and a `WAITING.md` the
    agent reads at the start.

## 2026-10-23 — Any model, any media vendor, read-only note folders

**What changed** (PRs #6–#14)
  - **Providers** (#6, #7, #8, #12, #13): MiniMax defaults to `MiniMax-M3` and the
    MiniMax Coding Plan subscription is supported; the Brain can talk to any
    OpenAI-compatible server (`OPENAI_BASE_URL`), and one endpoint can serve both
    chat and images. Provider errors are readable (retired model, no credit,
    bad key) and a failing plan call no longer wastes one call per agent. No
    `temperature` is sent to the Anthropic SDK 2.x.
  - **Media** (#11): the media pipeline is not tied to one vendor
    (`media.pick_provider`).
  - **Read-only note folders** (#10): extra folders of notes
    (`index_folders` / `memory_workspace` in `config.json`) are indexed read-only
    and re-indexed hourly.
  - The notes of the detected project now reach the Brain's context (#9).

## 2026-10-22 — Chat with your AI team

**What changed** (PRs #2–#5)
  - New `/chat` page (#3): talk to the Brain, the lead CEO or any executive. In
    team mode the CEO picks one to three agents, they answer in parallel in live
    cards, and the CEO writes the final answer (N+2 model calls per turn).
  - The desktop app has Back / Home controls on every page and a **Go** menu (#5).
  - Fix: every Teams load added the built-in AI squad again (#4).
  - The test suite no longer touches your real data folder (#2).

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
