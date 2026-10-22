# ConductLoop Desktop

Native macOS / Windows / Linux wrapper for [ConductLoop](../) — opens a window pointing at the local server (`http://localhost:7777`).

## Status

- **v1.0 (current)**: dev build, unsigned, macOS first.
- **v1.1 (next)**: code signing + notarization.
- **v1.2 (planned)**: bundled Python backend as Tauri sidecar (no need to run `make serve` separately).

## Stack

- Tauri 2.x (Rust + webview)
- Reuses the existing ConductLoop web app (no rewrite)
- Binaries: ~5-15 MB per platform (vs 80-150 MB for Electron)
- Auto-update: `tauri-plugin-updater` (planned, post-launch)

## Development

### Prerequisites
- macOS 10.15+, Windows 10+, or Linux
- Rust 1.77+ (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`)
- Node.js 20+ (for `@tauri-apps/cli`)
- Apple Developer ID cert (for code signing, not needed for dev builds)

### Quick start (dev mode)

```bash
# Terminal 1: start the local server
cd ~/ops-dashboard && make serve

# Terminal 2: launch the desktop wrapper
cd ~/ops-dashboard/desktop
cargo tauri dev
```

The window will open pointing at `http://localhost:7777`.

### Build (unsigned, for local testing)

```bash
cd ~/ops-dashboard/desktop
cargo tauri build --debug
# → src-tauri/target/debug/bundle/macos/ConductLoop.app
open src-tauri/target/debug/bundle/macos/ConductLoop.app
```

### Build (release, signed — v1.1+)

```bash
cargo tauri build
# → src-tauri/target/release/bundle/macos/ConductLoop.dmg
```

Requires:
- `APPLE_ID` (Apple ID email)
- `APPLE_PASSWORD` (app-specific password from appleid.apple.com)
- `APPLE_TEAM_ID` (your Apple Developer Team ID)
- Apple Distribution cert in keychain (✅ installed)

## Project layout

```
desktop/
├── package.json              # @tauri-apps/cli
├── src-tauri/
│   ├── Cargo.toml            # Rust deps
│   ├── tauri.conf.json       # Tauri 2 config
│   ├── build.rs              # tauri-build hook
│   ├── entitlements.plist    # macOS JIT/network
│   ├── capabilities/
│   │   └── default.json      # Tauri 2 permissions
│   ├── icons/                # 32, 128, 128@2x, icns, ico
│   ├── loading/
│   │   └── index.html        # splash/loading screen
│   └── src/
│       ├── main.rs           # entry point
│       └── lib.rs            # app setup (navigate to localhost:7777)
└── README.md
```

## Notes

- The current scaffold uses icons from `ev-quant-lab/desktop/src-tauri/icons/` as a placeholder. Generate ConductLoop-specific icons before v1.0 launch.
- v1.0 expects the server to be running separately. v1.2 will bundle the Python backend as a sidecar.
- The release build is configured for size optimization: `opt-level="s"`, `lto=true`, `strip=true`.
