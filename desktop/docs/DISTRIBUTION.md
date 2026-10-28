# ConductLoop Desktop — Distribution

How users get the ConductLoop macOS app and how it auto-updates.

## Requirements

- **macOS 13 (Ventura) or later** (`minimumSystemVersion` `13.0` in
  `desktop/src-tauri/tauri.conf.json`), Apple Silicon.
- The in-app browser (agent browser tabs) needs **macOS 14 (Sonoma) or later**;
  on macOS 13 the app launches and works, and the browser is disabled at
  runtime. The Network.framework symbols it needs are weak-linked, and
  `release-desktop.sh` aborts the release if they are strong.
- `tests/test_macos_minimum.py` keeps these numbers identical in
  `tauri.conf.json`, the READMEs, this file and the release-notes template.

## Channels

### 1. Direct download (primary)

The .dmg is hosted at **GitHub Releases** (`EVMCOD/conductloop-releases`).
The website `https://conductloop.com/download` links to the `latest/download`
asset URL.

```
https://github.com/EVMCOD/conductloop-releases/releases/download/v<VERSION>/ConductLoop-<VERSION>.dmg
https://github.com/EVMCOD/conductloop-releases/releases/latest/download/ConductLoop-macos-aarch64.dmg
```

**Why GitHub Releases (not R2 / not S3)?**
- Free, zero ops
- CDN-backed, fast everywhere
- Releases live in their own repo (`conductloop-releases`), so this source repo stays free of binaries

### 2. Auto-update (built-in)

The app includes `tauri-plugin-updater`. On startup, it checks:

```
https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json
```

If a newer version is published, the user sees a banner in the settings page
("Update available — restart to apply"). The auto-install only runs on
explicit user action (`install_update` Tauri command). We never yank a
running session without consent.

**Signing key**: kept by the maintainer outside this repository
(`~/.conductloop-keys/conductloop.key`, ed25519, no password).
The **public** key is baked into `tauri.conf.json`. Loss of the private key
= all installed apps stop receiving updates permanently.

The embedded key id is pinned in `desktop/UPDATER_KEY_ID` (currently
`770EA4C292DC6BD2`). CI fails if `tauri.conf.json` drifts from it, and
`release-desktop.sh` refuses to build if the signing `.pub` (or, after
signing, the produced `.sig`) has a different key id
(`scripts/check_updater_pubkey.py`; it only reads public material).

#### Known issue: existing installs (v1.2.0 and earlier) must be reinstalled once

v1.1.1 and v1.2.0 were built with updater pubkey `D68774E13CB63DED` (v1.1.0
with `CADDC7E4879ACD78`), but releases were signed with other keys
(`770EA4C292DC6BD2` for v1.2.0, `F9F7C0E2E9DA9D4D` for v1.1.1). The only
updater private key available is `770EA4C292DC6BD2`, so those installs
**cannot auto-update**: their updater rejects every signature we can produce.

Users on v1.1.x or v1.2.0 (and older) must **download and install the DMG
once** from <https://conductloop.com> (or `ConductLoop-macos-aarch64.dmg` on
the [latest release](https://github.com/EVMCOD/conductloop-releases/releases/latest)).
From the first release built with the `770EA4C292DC6BD2` pubkey onwards,
auto-update works again. If the private key for `D68774E13CB63DED` is ever
found, a one-off update signed with it would migrate v1.1.1/v1.2.0 installs
without a manual reinstall.

## Build pipeline

Before releasing, run the dry-run pre-flight check (`python3 scripts/release_check.py
--version <VERSION>`; see [docs/RELEASING.md](../../docs/RELEASING.md)). It publishes nothing.

A single script does the full release:

```bash
./scripts/release-desktop.sh <VERSION>      # e.g. 1.2.1
```

It needs your own Apple Developer ID, App Store Connect API key and updater
key (copy `scripts/release.local.env.example` to `scripts/release.local.env`);
none of them is in this repository, and it is run by hand on a Mac — CI never
signs or publishes.

Steps (see script for details):

0. Check the updater pubkey key id against the signing `.pub` and
   `desktop/UPDATER_KEY_ID` (abort on mismatch)
1. Update version in `Cargo.toml` + `tauri.conf.json`
2. Build Python sidecar via PyInstaller
3. `cargo tauri build` → `.app` + `.dmg` + updater `.tar.gz`
4. Re-sign sidecar + .app with Developer ID + hardened runtime
   (Tauri's bundle script doesn't always apply the right cert)
4b. Notarize the `.app` on its own (zipped with `ditto`) and staple it —
   stapling only the DMG leaves the copied-out app without a local ticket,
   so a first launch with no network gets blocked by Gatekeeper
5. Re-bundle DMG via `bundle_dmg.sh` (now around an already-stapled app)
6. Notarize via `xcrun notarytool submit` (App Store Connect API key)
7. Staple ticket via `xcrun stapler staple` — both the `.app` and the `.dmg`
   should pass `xcrun stapler validate`
8. Sign updater `.tar.gz` with the ed25519 key, then check the `.sig` key id
9. Generate `update-macos-aarch64.json` manifest
10. Smoke-test the .app (sidecar must bind 127.0.0.1:7777)
11. Upload to GitHub release `v{VERSION}`

## Why not Mac App Store?

- Tauri + Python sidecar pattern (HTTP server on localhost:7777) doesn't
  survive the App Sandbox without major refactor (XPC service or app group)
- 15-30% commission without distribution benefit for a local-first tool
- Industry standard for local-first / dev-tool apps (Cursor, Raycast, Linear)

## Files & artifacts

| File | Purpose |
|------|---------|
| `desktop/src-tauri/Cargo.toml` | Tauri app + plugins (shell, updater); `build.rs` weak-links Network.framework and restricts app commands |
| `desktop/src-tauri/tauri.conf.json` | Bundle config, updater endpoints, pubkey |
| `desktop/src-tauri/capabilities/default.json` | Sidecar + updater permissions |
| `desktop/src-tauri/src/lib.rs` | Sidecar spawn, menus + update Tauri commands (`browser.rs`: in-app browser) |
| `desktop/UPDATER_KEY_ID` | Pinned id of the updater key (checked in CI and by the release script) |
| `desktop/src-tauri/binaries/conductloop-server-<triple>` | PyInstaller sidecar (built by `desktop/build-app.sh` / `release-desktop.sh`; not committed) |
| `~/.conductloop-keys/conductloop.key` | Updater signing key (PRIVATE) |
| `~/.conductloop-keys/conductloop.key.pub` | Updater signing key (PUBLIC) |
| your `AuthKey_XXXX.p8` (path in `scripts/release.local.env`) | Notarization API key |
| `scripts/release-desktop.sh` | End-to-end release script |
