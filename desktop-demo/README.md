# ConductLoop Demo (Desktop)

Trial / marketing build of [ConductLoop](https://conductloop.com). Wraps the
local server in a Tauri 2 window. Uses **fixture data only** — no connection
to your real vault, no personal information.

## What's in this folder

- `ConductLoop Demo.app` — the macOS app (built by `cargo tauri build`)
- `ConductLoop Demo Server.command` — launcher that starts the local server in demo mode
- `ConductLoop_1.0.0_aarch64.dmg` — distributable installer

## How to use (after building)

1. Double-click `ConductLoop Demo Server.command`
2. Wait for the terminal to show: `ops-dashboard (DEMO) serving on http://127.0.0.1:7777/`
3. Double-click `ConductLoop Demo.app`
4. The window opens with the demo dashboard — explore!

To stop: close the .app, then `Ctrl+C` in the terminal.

## What the demo shows

- **31 notes** (PARA, Zettelkasten, evergreen notes, daily journal entries, book notes, project plans, health, code)
- **8 repos** (fictional Mosaic platform)
- **12 URLs** (status pages for the fictional Mosaic stack)
- **8 activity events** (recent feed)
- **12 months of revenue** (mock MRR with seasonal dip)
- **6 code TODOs** (extracted from the mock repos)

All names, companies, and numbers are fictional.

## Build

```bash
# From the ops-dashboard root
cd ~/ops-dashboard
cargo tauri build --manifest-path desktop-demo/src-tauri/Cargo.toml --debug
# → desktop-demo/src-tauri/target/debug/bundle/macos/ConductLoop Demo.app
```

## Storage

Demo data lives in `~/.conductloop-demo/data.db` — completely separate from
the real `ops-dashboard/data/ops.db`. Delete the folder to reset.
