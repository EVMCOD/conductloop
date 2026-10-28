# ConductLoop Desktop

The native app for [ConductLoop](../): a [Tauri v2](https://v2.tauri.app/)
shell that starts the bundled Python server as a sidecar (`127.0.0.1:7777`) and
shows it in a native WebView. Distributed as a signed, notarized DMG for
Apple Silicon Macs; see [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md).

## Requirements

| What | Minimum |
|---|---|
| macOS (end users) | **macOS 13 (Ventura)** — `minimumSystemVersion` is `13.0` in `src-tauri/tauri.conf.json` |
| In-app browser (agent browser tabs) | **macOS 14 (Sonoma)** — on macOS 13 the app runs normally and the browser is disabled at runtime |
| Hardware | Apple Silicon (arm64) release builds |

The in-app browser needs Network.framework proxy symbols that only exist on
macOS 14+. They are linked weakly (`src-tauri/build.rs`; `scripts/release-desktop.sh`
fails the build if they are strong), so the same binary still launches on macOS 13.
`tests/test_macos_minimum.py` fails if these numbers drift apart between
`tauri.conf.json`, this README, the root README, `DISTRIBUTION.md` and the
release-notes template in `scripts/release-desktop.sh`.

## Stack

- Tauri 2.x (Rust + system WebView), plugins `shell` (sidecar) and `updater`
- The same web UI as `make serve` — no separate front-end
- Server sidecar frozen with PyInstaller (`conductloop_server.spec`, entry point `server_entry.py`)
- Pure-Rust destination policy for the in-app browser in `url-policy/` (own crate, tested separately)

## Development

Prerequisites for building the app: Rust (stable), Node.js 20+ (for
`@tauri-apps/cli`) and, on macOS, the Xcode command line tools. A **Developer
ID certificate and your own updater key are only needed to sign and publish**;
neither is in this repository.

The app always spawns its bundled sidecar, so `cargo tauri dev` needs the
sidecar binary in `src-tauri/binaries/` first (`desktop/build-app.sh` creates it,
see below), then:

```bash
cd desktop && npm install
npm run tauri dev
```

Local build with the real sidecar (unsigned, for testing):

```bash
cd <repo root>
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyinstaller
(cd desktop && npm install)
desktop/build-app.sh             # freezes the server, then runs `npm run tauri build`
open desktop/src-tauri/target/release/bundle/macos/ConductLoop.app
```

Signing, notarizing and publishing are handled by `scripts/release-desktop.sh`
(see [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md)); copy
`scripts/release.local.env.example` to `scripts/release.local.env` and fill in
your own values.

## Checks that run in CI (`.github/workflows/desktop.yml`)

- `cargo test`, `cargo fmt --check` and `cargo clippy -D warnings` for `url-policy/`
- `cargo check --locked` and `cargo test --locked` for `src-tauri/` on Linux
  (WebKitGTK). This does **not** exercise macOS WKWebView: anything specific to
  macOS still needs a manual test on a Mac.

## Project layout

```
desktop/
├── package.json              # @tauri-apps/cli
├── server_entry.py           # PyInstaller entry point of the sidecar
├── conductloop_server.spec   # PyInstaller spec
├── build-app.sh              # freeze sidecar + cargo tauri build (unsigned)
├── UPDATER_KEY_ID            # key id the updater pubkey must match (checked in CI)
├── docs/DISTRIBUTION.md      # channels, auto-update, release pipeline
├── url-policy/               # destination policy + filtering proxy (Rust, no WebKit)
└── src-tauri/
    ├── Cargo.toml, build.rs  # build.rs also restricts app commands (AppManifest)
    ├── tauri.conf.json       # bundle, updater endpoint + pubkey, minimumSystemVersion
    ├── entitlements.plist
    ├── capabilities/default.json
    ├── loading/index.html    # splash shown until the server answers
    └── src/                  # main.rs, lib.rs (sidecar, updater, menus), browser.rs and
                              # agent_*.rs/js (in-app browser), nav.js (Back/Home pill)
```

## Notes

- The release build is size-optimized (`opt-level="s"`, `lto=true`, `strip=true`).
