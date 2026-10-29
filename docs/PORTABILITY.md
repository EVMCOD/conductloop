# Portability audit: Linux (experimental) and the Windows base

ConductLoop ships for macOS only today (Apple Silicon DMG, v1.2.2). This is an
inventory of everything in the repository that assumes macOS, what a Linux
(experimental) and a Windows port would do about each item, the risk, and which
small PR should carry it. **This document changes no behaviour**: it is the
audit that comes before the port.

How to read it:

- Every claim cites a file and a function (or symbol) so it can be re-checked
  with `rg`. Line numbers are left out on purpose: they rot.
- **untested** means nobody has run it on a real Linux or Windows machine.
  Anything about Windows, the in-app browser on Linux/Windows, the keychain
  backends, the updater on Linux/Windows and the packaging steps is untested.
  The few facts that *were* checked on a real machine say so ("checked on Linux").
- Upstream facts (Tauri, wry, WebKitGTK, WebView2) were read in the vendored
  crate sources of the versions in `desktop/src-tauri/Cargo.lock` (tauri 2.12.5,
  tauri-runtime-wry 2.12.3, wry 0.57.0, tauri-plugin-updater 2.14.0) and can
  change.
- Two product rules apply to every item:
  1. **Never run agent code without an OS sandbox.** If a platform has no
     sandbox, execution is disabled and the user is told why.
  2. **Never store a provider API key in plain text.** If a platform has no
     keychain, the encrypted vault is the fallback, not a plain file. (Other
     generated secrets still fall back to a `0600` file today; see 1.4.)

## Summary

| # | Area | macOS today | Linux (experimental) | Windows (base) |
|---|------|-------------|----------------------|----------------|
| 1 | Secrets / keychain | `security` CLI | Secret Service, vault fallback | Credential Manager, vault fallback |
| 2 | Execution sandbox | `sandbox-exec` | `bwrap` already implemented; needs hardening | none: execution disabled |
| 3 | In-app browser | WKWebView + Network.framework proxy | **blocked** upstream (child webviews stack on GTK); disable | proxy untested; disable at first |
| 4 | Paths, signals, permissions | works | mostly works, untested | several real bugs (see 4.3) |
| 5 | Sidecar | `...-aarch64-apple-darwin` | `...-x86_64-unknown-linux-gnu` | `...-x86_64-pc-windows-msvc.exe` |
| 6 | Menu and shortcuts | app menu + File | **no File menu, so no Close Tab** | File menu exists |
| 7 | Updater | `.app.tar.gz` + macOS JSON | AppImage only | NSIS (MSI optional) |
| 8 | CI | macOS job | ubuntu-latest (already compiles) + bundle job | windows-latest compile only |
| 9 | Everything else | launchd, `say`, `open`, ... | mostly scripts and copy | scripts and copy |

Serious blockers, in the order they hurt:

1. **Windows: `parent_watch.alive()` would terminate the desktop app** (4.3).
2. **Windows: the "port already taken" guard does not work** because
   `SO_REUSEADDR` means something else on Windows (4.3).
3. **Linux: the in-app browser cannot work yet** because Tauri places child
   webviews in a vertical GTK box and ignores their bounds (3.2).
4. **Linux: the sandbox does not hide Unix sockets** (D-Bus session bus, agent
   sockets, X11). That is harmless today but must be fixed *before* a Secret
   Service backend exists (2.1). Checked on Linux.
5. **Windows: the app token and boot nonce are never generated** (`random_hex32`
   is `#[cfg(unix)]`), so a security feature silently turns off (4.3).
6. **Release channel risk:** the updater endpoint is `releases/latest`, so a
   Linux- or Windows-only release published as "latest" would hide the macOS
   manifest (7.3).

---

## 1. Keychain and secrets

### 1.1 What exists (macOS)

- `ops_dashboard/secrets_store.py`
  - `_keychain_enabled()` is true only when `sys.platform == "darwin"` and the
    `security` binary exists, and `OPS_DISABLE_KEYCHAIN` is not `1`.
  - Service name `_SERVICE = "com.conductloop"`; the account is the secret name.
  - `_security_bin()` returns `/usr/bin/security`. `_kc_get()` runs
    `find-generic-password -w`. `_kc_write()` feeds `security -i` through stdin
    with the value hex-encoded (so it never appears in `ps`), refuses lines
    over `_MAX_LINE` (4000) and verifies by reading back. `_kc_delete()` and
    `_kc_set()` (which deletes a stale item when a write fails) complete it.
  - `get_secret()` / `set_secret()`: keychain first, otherwise a file
    `.<name>.secret` in `secrets_dir()` written with mode `0600`
    (`os.open(..., 0o600)` then `os.replace`). `secrets_dir()` is
    `OPS_SECRETS_DIR` or the data directory.
  - `keychain_available()`, `keychain_get/set/delete()` are the keychain-only API;
    `get_or_create_secret()` is used for generated keys.
- `ops_dashboard/byok_keys.py`: provider keys live under the reference
  `byok.<provider>.default` (providers: anthropic, openai, minimax, gateway).
  `store_api_key()` tries the keychain, then `secret_vault`, then (last resort,
  flagged to the user) plaintext in `config.json`. `get_api_key()` reads env,
  then the keychain reference, then legacy plaintext. `migrate_to_keychain()`,
  `revert_to_config()`, `dismiss_prompt()` and `status()` drive the UI banner.
- `ops_dashboard/secret_vault.py`: the **existing encrypted fallback**. Backends
  are reported by `where()` as `keychain`, `encrypted` or `nowhere`. The
  encrypted file is `.vault` in `secrets_dir()`: Fernet over
  PBKDF2-HMAC-SHA256 (240k rounds), with a user passphrase kept in memory only
  (`unlock()` / `lock()`). Reads check the keychain first and then the vault;
  `set()` writes to the keychain and only falls through to the vault (raising
  `VaultLocked` when no passphrase is set) if there is no keychain or it
  refuses.
- `ops_dashboard/connector_tokens.py`: connector OAuth tokens. `_KeychainBackend`
  **refuses to work without a keychain** (`TokenStoreUnavailable`,
  `keychain_unavailable`, surfaced as HTTP 503 by `connector_oauth.py`):
  tokens are never written to a file. Its `delete()` has a second
  macOS-only branch that calls `security delete-generic-password` directly
  instead of going through `secrets_store`.
- `ops_dashboard/connectors_token/base.py` (`save_state()`) writes connector state
  files with mode `0600`; `ops_dashboard/db_connections.py` stores its Fernet key
  with `get_or_create_secret("fernet-key", ...)`, so without a keychain that key
  sits in a `0600` file next to the database.
- Tests never touch a real keychain: `tests/conftest.py` sets
  `OPS_DISABLE_KEYCHAIN`. A new backend must honour the same switch.

### 1.2 Linux proposal

- **Backend:** the freedesktop Secret Service (GNOME Keyring, KWallet's
  Secret Service bridge, KeePassXC). Two ways to talk to it:
  - the Python `keyring` library restricted to its `SecretService` backend, or
  - the `secret-tool` CLI from `libsecret-tools` (value on stdin, like the
    macOS path).
  Recommendation: **`keyring` pinned to explicit backends**, because the
  PyInstaller sidecar cannot assume `secret-tool` is installed. The Rust shell
  never reads secrets (Python owns them), so the `keyring` *crate* is not needed.
- **Never accept a weak `keyring` backend.** `keyring` can resolve to a `fail`,
  chainer or null backend, and `keyrings.alt` ships plaintext/obfuscated file
  backends. The selector must check the resolved class name against an
  allow-list (`SecretService.Keyring` on Linux, `WinVaultKeyring` on Windows)
  and must not depend on `keyrings.alt`.
- **Probe, with a timeout.** A headless box, a missing session bus or a locked
  collection makes calls block (an unlock prompt) or fail. `keychain_available()`
  must do a set/get/delete of a probe item in a worker thread with a timeout and
  cache the answer. No session bus means "no keychain", and the vault takes over.
- **Same names:** service `com.conductloop`, account = secret name. Reference
  names such as `byok.<provider>.default` do not change, so `byok_keys` and
  `secret_vault.brain_key_name()` keep working untouched.

### 1.3 Windows proposal

- **Backend:** Windows Credential Manager through `keyring`'s `WinVaultKeyring`
  (target `com.conductloop`/name). **untested.**
- **Size limit:** Credential Manager blobs are capped at
  `CRED_MAX_CREDENTIAL_BLOB_SIZE` (2560 bytes; `keyring` stores UTF-16, so
  roughly 1280 characters). Provider API keys fit; OAuth token bundles in
  `connector_tokens.py` may not. Either chunk values or send oversized values
  to the vault. This is the Windows analogue of the 4000-byte `_MAX_LINE` rule.
  **untested.**

### 1.4 How the backend is chosen, and how migration works

Proposed selector in `secrets_store.py` (one function, no change of public names):

```
_backend():
    OPS_DISABLE_KEYCHAIN=1          -> None
    sys.platform == "darwin"        -> "macos-security"   (today's code, unchanged)
    sys.platform startswith linux   -> "secret-service"   if probe() succeeds else None
    sys.platform == "win32"         -> "wincred"          if probe() succeeds else None
    anything else                   -> None
```

- `_kc_get`, `_kc_write`, `_kc_delete`, `_kc_set`, `keychain_available` and
  `keychain_get/set/delete` dispatch on that value. **Keep the private names**:
  `connector_tokens._KeychainBackend` looks up `_kc_write`/`_kc_delete` with
  `getattr` and will report "keychain unavailable" if they disappear.
- `None` means "no keychain": `byok_keys.store_api_key()` goes to
  `secret_vault` (encrypted, passphrase) and never to plaintext unless the user
  explicitly accepts the existing last-resort path. The plaintext `config.json`
  path stays as it is on every platform.
- **Migration.** Secrets never move between operating systems automatically; a
  user on a new OS re-enters keys. On one machine nothing is moved silently:
  reads already try the keychain and then the vault, and the existing
  consent-based flows (`byok_keys.migrate_to_keychain(consent=True)`,
  `revert_to_config`) are reused for "a keychain appeared later". Moving entries
  from `.vault` into a newly available keychain needs one new consent-based
  action. Whether the sync bundle (`sync_bundle.py`) should carry keys across
  machines is a product question this audit did not settle.
- **Connector tokens without a keychain** (a Linux server box, for instance)
  are unavailable today. Options: leave it (safe, documented in the UI) or give
  `connector_tokens` a vault-backed backend that works while the vault is
  unlocked. Recommendation: leave it for the first Linux PR, decide later.
- **Generated Fernet key** (`db_connections.py`) lands in a `0600` file when
  there is no keychain; on Windows that mode is meaningless (4.3). Decision
  needed: route it through the vault.

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done (`security` CLI) | missing; falls to file/vault | missing; falls to file/vault |
| Risk | - | Medium: D-Bus probe hangs, weak `keyring` backend | Medium: blob size limit, **untested** |
| PR | - | PR 3 | PR 10 |

---

## 2. Execution sandbox

### 2.1 What exists

`ops_dashboard/sessions/sandbox.py`:

- `detect_backend()` returns `"sandbox-exec"` on macOS (binary present and a
  probe command succeeds), `"bwrap"` on Linux (`bwrap` on `PATH` and the probe
  `--unshare-net --ro-bind / / --dev /dev --proc /proc true` succeeds), else
  `None`. `CONDUCTLOOP_SANDBOX=none` forces `None`.
- `bwrap_argv()` unshares net, ipc, pid and uts, uses `--die-with-parent` and
  `--new-session`, read-only binds `/`, hides `SENSITIVE_HOME_PATHS` and the
  ConductLoop data directory with tmpfs, and binds only the worktree and a
  temporary directory writable. `seatbelt_profile()` is the macOS equivalent.
- `run()` raises `SandboxUnavailable` when there is no backend unless
  `allow_unsandboxed` is passed. That flag is stripped by
  `sessions/runner.py` (`SessionRunner.__init__`) and
  `sessions/supervisor.py` (`Supervisor.__init__`), and `code_tools.py` calls
  `sandbox.run`, so **sessions cannot run unsandboxed**. This rule already holds
  and only needs to be kept on new platforms.
- CI covers Linux for real: `.github/workflows/ci.yml` installs `bubblewrap`,
  disables Ubuntu 24.04's AppArmor userns restriction
  (`kernel.apparmor_restrict_unprivileged_userns=0`) and runs with
  `CONDUCTLOOP_REQUIRE_SANDBOX=1`.

### 2.2 Linux

**Status: implemented and tested in CI; needs hardening and honest UX.**

- If `bwrap` is missing or the probe fails, `detect_backend()` is `None` and
  execution is **disabled** (already the behaviour). The UI must say why and how
  to fix it: install `bubblewrap`; on Ubuntu 24.04 an AppArmor profile or sysctl
  is needed for unprivileged user namespaces (the CI workaround is
  not something to ask end users to do blindly). A `.deb` can `Recommends:
  bubblewrap`; an AppImage cannot bundle a working `bwrap`. **untested** on a
  real desktop distribution.
- **Gap found (checked on Linux):** `--ro-bind / /` is recursive, so
  *filesystem* Unix sockets stay reachable from inside the sandbox. A probe
  using `bwrap_argv()` from this repository connected from inside the sandbox to
  a Unix socket outside it. `--unshare-net` only hides abstract sockets. That
  exposes, on a normal desktop, the D-Bus session bus
  (`$XDG_RUNTIME_DIR/bus`, hence the **Secret Service**), the SSH and GPG agent
  sockets under `$XDG_RUNTIME_DIR`, and the X11 socket in `/tmp/.X11-unix`.
  Hardening: `--tmpfs $XDG_RUNTIME_DIR` (and `/run/dbus`, `/tmp/.X11-unix`), and
  the Linux equivalents of `SENSITIVE_HOME_PATHS` (`~/.local/share/keyrings`,
  `~/.local/share/conductloop`, browser profiles). Today the list only names
  `.config/conductloop` and macOS `Library/...` paths. **This must land before
  any Secret Service backend**, otherwise a sandboxed command could read every
  API key from an unlocked keyring.
- `build_env()` hardcodes `/usr/bin:/bin` plus the caller's `path_dirs`; fine on
  FHS distributions, **untested** on NixOS or immutable distributions.
- The rest of the user's home is readable inside the sandbox on both platforms
  (read-only bind of `/`); that is a design choice of the current sandbox, not a
  portability bug.
- User-configured MCP servers are spawned by `ops_dashboard/mcp_client.py`
  (`StdioTransport`, `Popen(..., start_new_session=True)`) **outside** the
  sandbox on every platform. Different threat model, same on macOS; mentioned
  so it is not mistaken for sandbox coverage.

### 2.3 Windows

**Status: no sandbox exists. Code execution is disabled and the user is told.**

- Windows has no `bwrap`/`sandbox-exec` equivalent that is usable here (AppContainer,
  Job Objects and Windows Sandbox are different products and would be a
  project of their own). `detect_backend()` already returns `None` on
  `win32`, so `run()` raises `SandboxUnavailable`: execution is refused.
- Make the refusal explicit and visible: a platform flag in the backend status
  API, and a message in the sessions UI ("Code execution needs an OS sandbox;
  this platform has none yet"). Never offer `allow_unsandboxed`.
- Code that must not be *reachable* on Windows even though it is gated:
  `_limits()` (`resource.setrlimit`, POSIX only), `preexec_fn=` in `run()`
  (not supported on Windows: raises `ValueError`), `os.killpg(..., SIGKILL)`
  (does not exist on Windows). Put them behind a POSIX check so an import or a
  future refactor cannot turn them into a crash or, worse, a fallback path.
- `sessions/code_tools.py` `_atomic_write()` relies on `dir_fd`, `O_DIRECTORY`
  and `O_NOFOLLOW` (POSIX; `dir_fd` raises `NotImplementedError` on Windows).
  Its `.git` check (`_is_git_component()`: NFC, `casefold`, trailing dots and
  spaces) already knows about case-insensitive filesystems but does not know
  8.3 short names or NTFS alternate data streams. Therefore **whole code
  sessions are disabled on Windows**, not just `run_command`. **untested.**

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done | done (CI); hardening missing | disabled |
| Risk | - | Medium: sockets, userns on Ubuntu 24.04 | Low if disabled; High if anything runs unsandboxed |
| PR | - | PR 2 | PR 8 (disable + message) |

---

## 3. In-app browser

### 3.1 What exists (macOS 14+)

- `desktop/src-tauri/src/browser.rs`: each tab is a **child webview**
  (`Window::add_child`, needs tauri's `unstable` feature) built in `open_tab()`
  with `.proxy_url(...)`, `.incognito(true)`, `.on_navigation`,
  `.on_new_window`, `.on_download(false)`, `.on_permission_request(Deny)`.
  `start_proxy()` runs the pure-Rust destination policy in
  `desktop/url-policy/` (`conductloop_url_policy::proxy::spawn`), so private and
  loopback destinations are refused at the proxy, not just in the UI.
- The proxy needs Network.framework symbols, so `Cargo.toml` enables tauri's
  `macos-proxy` feature and `desktop/src-tauri/build.rs` weak-links the
  framework (`-Wl,-weak_framework,Network`). `os_supported()` runs `sw_vers`
  and requires major >= 14; `open_tab()` returns `macos_14_required` otherwise,
  and `ops_dashboard/web/browser.html` renders that string.
- `desktop/src-tauri/src/agent_bridge.rs` + `ops_dashboard/sessions/browser.py`
  (`BrowserBridge`) talk over an `AF_UNIX` socket
  (`CONDUCTLOOP_BROWSER_SOCK`, `CONDUCTLOOP_BROWSER_TOKEN`); the Rust side is
  `#[cfg(unix)]` (`start_agent_bridge`, `bridge_token` in `lib.rs`).
  `BrowserUnavailable` says "only available in the ConductLoop desktop app
  (macOS 14+)".
- `measure_title_bar()` in `browser.rs` uses `objc2-app-kit` on macOS and
  returns `0.0` elsewhere.

### 3.2 Linux (WebKitGTK)

- **Current behaviour is wrong, not "disabled".** `os_supported()` returns `true`
  on every non-macOS target, so on Linux the browser would try to open.
- **Blocker upstream.** `tauri-runtime-wry` builds child webviews on Linux with
  `build_gtk(window.default_vbox())`, a `gtk::Box`. Position and size are
  ignored (webviews stack vertically) and `set_bounds` is a no-op there. This is
  tauri issue #10420; a fix using `GtkFixed` (tauri PR #15704) was open when
  this audit was written. wry's `build_as_child` is X11-only, there is no
  Wayland support. ConductLoop's tab model (a tab bar in the main webview plus
  positioned child webviews) cannot work until that lands.
- **Proxy: possible, unverified.** wry's WebKitGTK backend
  (`src/webkitgtk/mod.rs`) applies `NetworkProxySettings` in
  `NetworkProxyMode::Custom` on the webview's data manager; with `incognito(true)`
  wry creates an ephemeral web context per webview, so the proxy would be
  per-tab. Whether WebKitGTK sends loopback traffic through a custom proxy (the
  policy relies on that to see *every* request) is **untested**.
- **Request interception:** the policy does not need it; it is enforced at the
  proxy plus `.on_navigation`. WebKitGTK offers no per-webview request filter
  that replaces the proxy.
- **Proposal:** make `os_supported()` return `false` on Linux, with a distinct
  reason (`browser_unsupported_platform`), and show "not available on this
  platform yet" in `web/browser.html`. Re-enable behind a build flag once the
  Tauri fix is released and the proxy behaviour is checked on real WebKitGTK
  versions.

### 3.3 Windows (WebView2)

- Child webviews work in `tauri-runtime-wry` on Windows. The proxy is passed as a
  Chromium switch (`--proxy-server=...`) when the WebView2 environment is
  created. Tauri's own docs warn that webviews with different browser arguments
  must use **different data directories**, and `additional_browser_args` is not
  available on macOS/Linux.
- Chromium bypasses the proxy for loopback by default. The policy needs
  `--proxy-bypass-list=<-loopback>`, and setting browser arguments replaces
  wry's default arguments. Whether per-tab proxy plus data-dir isolation holds
  with incognito is **untested**.
- `AF_UNIX`: Python on Windows does not expose `socket.AF_UNIX`, and
  `agent_bridge.rs` is `#[cfg(unix)]`, so the agent bridge does not exist on
  Windows. A named pipe or an authenticated loopback port would be needed.
- **Proposal:** disable the browser on Windows in the first port. Revisit after
  the proxy is validated on a real machine.

### 3.4 Rule

If a platform has no *verified* equivalent of the network filter, the browser is
**disabled** there, never enabled without it. The Python side must stay
consistent: `BrowserUnavailable` should not say "macOS 14+" on other platforms.
`tests/test_macos_minimum.py` greps `browser.rs` for `major >= 14`; keep that
expression intact when `os_supported()` changes.

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done (14+) | broken upstream | not implemented |
| Risk | - | High: looks enabled today | High: proxy and isolation unverified |
| PR | - | PR 1 (disable) | PR 8 (disable) |

---

## 4. Paths, processes, permissions

### 4.1 Data and config paths

`ops_dashboard/app_paths.py` is the single source (`db.py` `_default_data_dir()`,
`secrets_store.secrets_dir()` and `sessions/worktrees.py` all go through it).
`data_dir()` resolves in tiers: `OPS_DATA_DIR`; the repo's `data/` when it
exists (development); `~/ops-dashboard/data` when it already holds an `ops.db`
(legacy); otherwise the per-platform default:

| Platform | Default data dir |
|---|---|
| macOS | `~/Library/Application Support/com.conductloop.desktop/data` |
| Linux | `$XDG_DATA_HOME/conductloop/data`, else `~/.local/share/conductloop/data` |
| Windows | `%LOCALAPPDATA%/ConductLoop/data` |

`config_path()` honours `OPS_CONFIG_PATH`. **Paths already cover all three
platforms in code; none of it has run on a real Linux desktop or Windows machine
except through the Linux CI tests.** Notes:

- The `README.md` ("Linux `~/.local/share/conductloop/ops.db`") and the comment
  in `db.py` disagree with `app_paths.data_dir()`, which returns
  `.../conductloop/data`; the code is authoritative. Fix both in PR 1.
  Also, `XDG_DATA_HOME` set to an *empty* string yields a relative path
  (`os.environ.get` returns `""`); treat empty like unset.
- Tauri's own `app_data_dir` is named `com.conductloop.desktop` on every
  platform, while Python uses `conductloop` / `ConductLoop`. Today Rust only
  uses its directory for the agent bridge socket; keep it that way or pass the
  directory to the sidecar.
- `conductloop_server_entry.py` (legacy entry) repeats the data-dir logic; the
  frozen sidecar uses `desktop/server_entry.py`. Remove the duplicate in a later
  cleanup, not in a port PR.
- Other `~`-relative defaults are plain `Path.home()` joins and work anywhere:
  `db.py` `DEFAULT_VAULT`, `plugins.py`, `indexers/repos.py`. Hardcoded
  macOS locations are listed in section 9.

### 4.2 Other portability checks

| Topic | Finding | Linux | Windows |
|---|---|---|---|
| Atomic writes | `os.replace` after a temp file (`secrets_store.set_secret`, `secret_vault._write_raw`) | fine | `os.replace` raises `PermissionError` if the target is open elsewhere; add a short retry. **untested** |
| Text encoding | most file calls pass `encoding=`; `text=True` appears in about 14 `subprocess` calls under `ops_dashboard/` (not individually checked for `encoding=`); `indexers/github.py` and `indexers/live_urls.py` use bare `open()` | UTF-8 locale assumed | locale code page (cp1252/OEM) unless UTF-8 mode is on. Set `PYTHONUTF8=1` for the sidecar (in the Tauri spawn env or the spec) and pass `encoding=` in the leftovers. **untested** |
| Line endings | no `.gitattributes`; indexers use `splitlines()` | fine | git `core.autocrlf` may rewrite files in worktrees and vault notes. Add `.gitattributes` with `* text=auto eol=lf` for the repo only; user vaults are not ours to touch. **untested** |
| Spaces in paths | argv lists everywhere, no shell strings in the Python code | fine | `%LOCALAPPDATA%\ConductLoop` is fine; user names with spaces/non-ASCII are the usual hazard. **untested** |
| Case sensitivity | macOS default is case-insensitive, Linux is case-sensitive; `sessions/paths.py` and `code_tools.py` (`casefold`) already compare conservatively | document: two files differing by case can coexist | case-insensitive like macOS |
| Long paths (Windows) | worktrees live under the data dir: `...\ConductLoop\data\worktrees\<repo>\<branch>` | n/a | MAX_PATH 260. Needs the `LongPathsEnabled` registry setting plus a `longPathAware` manifest on the sidecar (PyInstaller does not add it by default) and `core.longpaths` for git. **untested** |
| `localhost` | `BACKEND_URL` is `http://localhost:7777` while the server binds `127.0.0.1` | fine | Windows may resolve `localhost` to `::1` first; connection falls back, costs time. Prefer `127.0.0.1` on Windows. **untested** |

### 4.3 Process and permission issues

1. **Windows: `parent_watch.alive()` kills the app.**
   `ops_dashboard/parent_watch.py` `alive(pid)` calls `os.kill(pid, 0)`. On
   Windows, CPython treats any signal other than `CTRL_C_EVENT` /
   `CTRL_BREAK_EVENT` as "terminate the process" (`TerminateProcess`, exit code =
   the signal number). The sidecar passes the **desktop app's pid**
   (`CONDUCTLOOP_PARENT_PID`, started from `desktop/server_entry.py`), so its
   first liveness check would terminate the app. Rewrite with
   `ctypes` (`OpenProcess` + `WaitForSingleObject`) or `psutil`, and add a test
   that runs on Windows CI. Behaviour is from the Python documentation;
   **untested** on Windows. Linux and macOS are unaffected, and the same
   mechanism is what cleans up the PyInstaller one-file child when Tauri kills
   only the bootloader.
   **Fixed in PR 8** (`parent_watch._alive_windows`): `OpenProcess` with
   `SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION` plus a 0 ms
   `WaitForSingleObject`, never a signal. Only "no such process" (error 87) means
   gone; any other failure counts as alive. POSIX keeps `os.kill(pid, 0)`
   unchanged. Unit-tested with a fake `kernel32`; the real-process test runs only
   on Windows. **untested** on a real Windows machine until the `windows-latest`
   job (PR 9) runs it.
2. **Windows: the "port already in use" guard is defeated.**
   `server.py` `_bind_server()` relies on a second bind failing (a local process
   that grabs port 7777 first would receive the boot nonce). `ThreadingHTTPServer`
   sets `SO_REUSEADDR`; on Windows that option **allows** two processes to bind
   the same port, so no error is raised. Windows needs `SO_EXCLUSIVEADDRUSE`
   (and no `SO_REUSEADDR`). **untested.**
   **Fixed in PR 8** (`server._LocalHTTPServer`, used by `_bind_server`): on
   `win32` only, `allow_reuse_address` is switched off and `SO_EXCLUSIVEADDRUSE`
   is set before binding; elsewhere it is exactly `ThreadingHTTPServer`. Servers
   created directly (tests) are not affected. **untested** on real Windows.
3. **Windows: no app token, boot nonce or instance id.**
   `lib.rs` `random_hex32()` reads `/dev/urandom` under `#[cfg(unix)]` and
   returns `None` elsewhere, so the sidecar environment variables
   `CONDUCTLOOP_APP_TOKEN`, `CONDUCTLOOP_APP_BOOT` and `CONDUCTLOOP_INSTANCE_ID`
   are simply not set and the server "keeps its previous behaviour". Use the
   `getrandom` crate (or `BCryptGenRandom`). Do not ship Windows without it.
   **Fixed in PR 8:** a `#[cfg(windows)]` branch calls `getrandom::fill`;
   `getrandom 0.3` is a direct dependency on Windows only
   (`[target.'cfg(windows)'.dependencies]`) and was already in `Cargo.lock`
   through `tauri`. The macOS/Linux branch is unchanged. This box cannot compile
   Rust, so it is **untested** until CI compiles it for Windows (PR 9).
4. **Windows: file permissions.** `chmod(0o600)` and `os.open(..., 0o600)` are
   no-ops (only the read-only bit is honoured). Call sites: `secrets_store.py`,
   `secret_vault.py`, `connectors_token/base.py`, `brain.py`, `sync_bundle.py`,
   `learning/store.py`. Files under `%LOCALAPPDATA%` inherit an ACL limited to
   the user, SYSTEM and Administrators, which is acceptable for non-secret
   files. For anything secret, use the vault (encrypted) rather than relying on
   ACLs; an optional `icacls` tightening can come later. **untested.**
   **PR 8:** the `chmod` calls were already wrapped in `try/except OSError`, so
   nothing failed on Windows; they now go through one helper,
   `ops_dashboard/fileperms.restrict_to_owner()`, which applies the mode on
   macOS/Linux exactly as before and does nothing on Windows (returns `False`,
   never raises). The `os.open(..., 0o600)` calls are left as they are (the mode
   is ignored on Windows). Nothing secret may depend on this protection there.
5. **Signals and process groups.** The server installs no signal handlers; it
   ends when Tauri kills the sidecar (`ServerProcess` in `lib.rs`, on window
   destroy) or when `parent_watch` fires and `server_entry.py` calls
   `os._exit(0)`. POSIX process groups (`start_new_session=True`,
   `os.killpg`) are used in `sessions/sandbox.py` and `mcp_client.py`; on
   Windows `start_new_session` is ignored and `killpg` does not exist.
   Terminating a tree on Windows needs a Job Object or `taskkill /T`.
   `CREATE_NO_WINDOW` is already set by `tauri-plugin-shell` for the sidecar.
   `CTRL_BREAK_EVENT` is only useful for a console child with a new process
   group and is not needed for the sidecar. **untested.**
6. **File descriptors.** `media._ensure_fd_headroom()` imports `resource` inside
   a `try/except`; it is a no-op where `resource` is missing (Windows).
7. **Unix sockets in Rust.** `agent_bridge.rs` binds a Unix socket with `0700`
   directory and `0600` socket permissions; Linux works as on macOS
   (**untested**), Windows has no bridge (3.3).

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done | works in CI for Python paths; app untested | items 1-4 fixed in PR 8 (untested on real Windows); 5-7 open |
| Risk | - | Low | High (items 1-3) |
| PR | - | PR 1 | PR 8 |

---

## 5. PyInstaller sidecar per platform

- **Spec and entry:** `desktop/conductloop_server.spec` (onefile, `console=True`,
  `collect_all` for `ops_dashboard`, `fastembed`, `onnxruntime`; `torch` is
  excluded) and `desktop/server_entry.py`. The repository-root
  `conductloop-server.spec` and `conductloop_server_entry.py` are the legacy
  onedir pair.
- **Naming:** Tauri's `externalBin` is `binaries/conductloop-server` in
  `tauri.conf.json`; the file on disk must be
  `desktop/src-tauri/binaries/conductloop-server-<target-triple>` plus `.exe`
  on Windows. Capabilities refer to the sidecar by `binaries/conductloop-server`
  and need no change.

| Platform | Target triple | File name |
|---|---|---|
| macOS (today) | `aarch64-apple-darwin` | `conductloop-server-aarch64-apple-darwin` |
| Linux x86_64 | `x86_64-unknown-linux-gnu` | `conductloop-server-x86_64-unknown-linux-gnu` |
| Linux arm64 (later) | `aarch64-unknown-linux-gnu` | `conductloop-server-aarch64-unknown-linux-gnu` |
| Windows x86_64 | `x86_64-pc-windows-msvc` | `conductloop-server-x86_64-pc-windows-msvc.exe` |

- **Where the macOS one is built:** `scripts/release-desktop.sh` runs
  `.venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm --distpath dist
  --workpath build/pyi`, copies `dist/conductloop-server` to
  `desktop/src-tauri/binaries/conductloop-server-aarch64-apple-darwin`, then
  runs `cargo tauri build`. `desktop/build-app.sh` detects the triple with
  `rustc -vV` (defaulting to `aarch64-apple-darwin`) and is the dev-time
  equivalent. `desktop/src-tauri/build-sidecar.sh` is an older, macOS-only
  script (hard-coded triple, `/Applications` wrapper, a `Python.framework`
  path); do not extend it.
- **Linux:** the binary links the build machine's glibc, so build on the
  *oldest* runner you intend to support (an `ubuntu-22.04` image rather than
  `-latest`) so it also runs on older distributions. Python 3.14 plus
  `onnxruntime`/`fastembed` wheels for Linux x86_64 are assumed available.
  **untested.**
- **Windows:** the venv layout is `.venv\Scripts\pyinstaller.exe`; a onefile
  `.exe` extracts to a temp dir on every start (slow start, antivirus
  false positives are common). `console=True` is fine because the shell spawns it
  with `CREATE_NO_WINDOW`. Without Authenticode signing SmartScreen will warn.
  **untested.**
- **Do not touch `scripts/release-desktop.sh`.** Add new scripts (new files, for
  example `scripts/build-sidecar-linux.sh` (new)) that take the triple as a
  parameter and share the PyInstaller invocation. Build bundles with
  `cargo tauri build --bundles appimage,deb` (Linux) or `--bundles nsis`
  (Windows) so the macOS `bundle.targets` (`["dmg", "app"]`) in
  `tauri.conf.json` stays as it is.
- Icons: `desktop/src-tauri/icons/` has `32x32.png`, `128x128.png`,
  `128x128@2x.png`, `icon.icns` and `icon.ico`, which is what the Linux and
  Windows bundlers need. A 512 px PNG is optional. **untested** in a real
  bundle.

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done | not built | not built |
| Risk | - | Medium (glibc, onnxruntime size) | Medium (AV, long paths) |
| PR | - | PR 4 | PR 11 |

---

## 6. Native menu and shortcuts

`desktop/src-tauri/src/lib.rs` `app_menu()` starts from `Menu::default()`,
then:

- inserts **Check for Updates...** after "About" only when
  `cfg!(target_os = "macos")` and the submenu index is 0 (the app menu);
- removes every predefined "Close Window" item;
- adds **Close Tab** (`CmdOrCtrl+W`) and **Close Window**
  (`CmdOrCtrl+Shift+W`) to the submenu titled "File";
- adds a **Go** menu: Back `CmdOrCtrl+[`, Forward `CmdOrCtrl+]`, Home
  `CmdOrCtrl+Shift+H`.

Findings (from the tauri 2.12.5 source of `Menu::default`):

- **Linux has no "File" submenu in the default menu**, so Close Tab and Close
  Window are never added and, because the predefined "Close Window" in the
  Window menu is removed, **there is no close shortcut at all**. Fix: create
  the File submenu explicitly on Linux.
- **Windows** has a File menu (Close Window, Quit), so the code works, but there
  is no app menu and therefore no "Check for Updates...". Add it to Help on
  Linux and Windows.
- `CmdOrCtrl` maps to Ctrl on both. The web UI already handles
  `metaKey || ctrlKey` (`web/app.js`, `web/dialog.js`, `web/note.html`,
  `web/boardroom-thread.html`), but hints in `web/index.html` print the command
  symbol and need platform-aware text. Alt/Option and `Ctrl+[` conflicts with
  some Linux input methods are **untested**.
- On Linux and Windows the menu bar is inside the window and takes vertical
  space; `measure_title_bar()` returns `0.0` there. Matters only when the
  browser is enabled (section 3). **untested.**

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done | missing close shortcuts, no updates item | updates item missing |
| Risk | - | Low | Low |
| PR | - | PR 5 | PR 11 |

---

## 7. Updater

**This section only describes; it proposes no change to the updater now.**

### 7.1 Today

- `tauri.conf.json` `plugins.updater`: one endpoint,
  `https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json`,
  the pinned public key (minisign id `770EA4C292DC6BD2`, recorded in
  `desktop/UPDATER_KEY_ID` and checked by `scripts/check_updater_pubkey.py` and
  `scripts/release_check.py`) and `windows.installMode = "passive"`.
- `bundle.createUpdaterArtifacts` is `true`. `updates.rs` has
  `check_after_startup()` (release builds, ~20 s after start, silent) and
  `check_from_menu()`; `lib.rs` exposes `check_for_update` and `install_update`.
- `scripts/release-desktop.sh` writes `update-macos-aarch64.json` (platform key
  `darwin-aarch64`) and publishes to `EVMCOD/conductloop-releases`, tag
  `v<version>`. The v1.2.2 release assets are: `ConductLoop-1.2.2.dmg`,
  `ConductLoop-macos-aarch64.dmg`, `ConductLoop-1.2.2.app.tar.gz`, its `.sig`,
  and `update-macos-aarch64.json`.

### 7.2 Per platform (tauri-plugin-updater 2.14.0)

| Platform | Artifact | Signature | Manifest key | Notes |
|---|---|---|---|---|
| macOS | `.app.tar.gz` | minisign `.sig` | `darwin-aarch64` | today |
| Linux | `.AppImage` (updater artifact) | minisign `.sig`, same key | `linux-x86_64-appimage` | replaces the AppImage in place |
| Linux `.deb` | `.deb` | n/a | **omit** | updates through the package manager or a manual download; the plugin *can* install `.deb` via `pkexec`/`sudo`, but we do not want a privilege prompt |
| Windows | NSIS `-setup.exe` (MSI optional) | minisign `.sig`, same key | `windows-x86_64-nsis` | `installMode: passive` already set |

- **Same key everywhere.** Updater signatures are minisign and independent of the
  OS; the same private key signs all platforms and the same public key stays in
  `tauri.conf.json`. Authenticode (Windows) and notarization (macOS) are
  separate and unrelated to this key. No Authenticode certificate exists, so
  SmartScreen will warn on Windows installers. **untested.**
- **Manifest lookup.** The plugin takes the first endpoint that returns valid
  JSON; if the file has no entry for the running target it reports
  `TargetsNotFound`, it does **not** try the next endpoint. It looks for
  `{os}-{arch}-{installer}` and then `{os}-{arch}`. URL templates support
  `{{target}}`, `{{arch}}`, `{{current_version}}` and `{{bundle_type}}`.
- **"`.deb` without auto-update"** therefore means: the Linux manifest contains
  only `linux-x86_64-appimage`; a `.deb` install gets a clean "update through
  your package manager or download the new version" message instead of an error.
  The UI must handle `TargetsNotFound` as that message (today it would show an
  error from the menu and nothing at startup). **untested.**
- **Per-platform latest JSON in `conductloop-releases`.** One manifest per
  platform next to the macOS one: `update-linux-x86_64.json` and
  `update-windows-x86_64.json`, uploaded to the same release tag. **Keep the
  macOS file name (`update-macos-aarch64.json`) forever**: every installed 1.2.x
  app requests exactly that URL, so renaming it for a templated endpoint such as
  `update-{{target}}-{{arch}}.json` would strand them. The Linux and Windows
  endpoints are therefore selected at build time for those platforms only (for
  example a config override passed to `cargo tauri build`), and the macOS value
  in `tauri.conf.json` is not edited. How to inject that override is an open
  design question for PR 7 and PR 11. **untested.**

### 7.3 Release-channel risk

Installed macOS apps ask `releases/latest/download/update-macos-aarch64.json`.
GitHub's "latest" is the newest non-prerelease **release**, not the newest
asset. A release that contains only Linux or Windows assets and is marked
latest would make that URL a 404 and silently stop macOS updates. Rules for any
non-macOS release:

- publish Linux and Windows assets **in the same tag** as the macOS ones, or
- publish them as **prereleases** until the combined release is cut.

Never mark a non-macOS-only release as latest. Add a check for this to
`scripts/release_check.py` in a later PR (it is not part of this audit; the
macOS release script is not touched).

| | macOS | Linux | Windows |
|---|---|---|---|
| Status | done | not built | not built |
| Risk | - | Medium: TargetsNotFound UX; AppImage-only | Medium: no Authenticode |
| PR | - | PR 7 | PR 11 |

---

## 8. CI

Today:

- `.github/workflows/ci.yml` (`ubuntu-latest`, Python 3.14): `pytest -m "not live"`
  with coverage, bubblewrap and ffmpeg installed, headless-Chrome tests. This
  is why the Python backend is already exercised on Linux.
- `.github/workflows/desktop.yml`: `url-policy` (ubuntu), `desktop-rust`
  (ubuntu-latest; installs `libwebkit2gtk-4.1-dev libgtk-3-dev
  libayatana-appindicator3-dev librsvg2-dev`, creates a placeholder sidecar named
  with the host triple, runs `cargo check --locked --all-targets` and
  `cargo test --locked`) and `desktop-macos` (`macos-latest`). It only
  triggers on `desktop/**` and the workflow file, so **a docs-only PR does not
  run it**. So the Linux Rust crate already compiles in CI.
- `tests/test_workflows_guard.py` allows only literal GitHub-hosted runner
  labels (`ubuntu|macos|windows-...`), requires `timeout-minutes` and requires
  every third-party action to be pinned by commit SHA with a version comment.
  New jobs must follow this; look up the SHAs when writing them rather than
  guessing.

Proposed additions (each its own PR, none touches `desktop-macos`):

1. **`desktop-linux-bundle` on `ubuntu-22.04`** (older glibc): same apt packages as
   `desktop-rust` plus `libfuse2` where needed, build the sidecar with the Linux
   triple, run `cargo tauri build --bundles appimage,deb`, upload the results as
   workflow artifacts with **`retention-days: 3`**. Never publish them. Run only
   on `workflow_dispatch` and on PRs that touch the packaging scripts, to keep
   minutes low. **untested.**
2. **`desktop-windows` on `windows-latest`, compile only:** `cargo check --locked
   --all-targets` for `desktop/src-tauri` (with a placeholder
   `conductloop-server-x86_64-pc-windows-msvc.exe`) and `cargo test --locked` for
   `desktop/url-policy`, plus a small Python job running the Windows-safe subset
   of `tests/` (`test_parent_watch.py`, `test_app_paths.py`, `test_secrets_store.py`)
   once the code supports it. No bundling, no signing. **untested.** Expect the
   first run to fail until the `#[cfg(unix)]` paths compile cleanly.
3. If a job fails in 2-3 seconds, or the log mentions a spending limit, **stop
   and ask**: that is a billing/plan problem, not a code problem.

---

## 9. Everything else that assumes macOS

| Item | Where | Linux | Windows | Risk | PR |
|---|---|---|---|---|---|
| Text-to-speech `say` | `media_backends.py` `MacSayProvider` (provider key `macos_say`) | already skipped when `say` is missing; Piper is the portable provider | same | Low | PR 1 (show only available providers) |
| Tailscale discovery | `server.py` `_tailscale_ips()`: `/Applications/Tailscale.app`, `/opt/homebrew/bin`, `ifconfig` fallback | `tailscale` on `PATH` works; `ifconfig` may be missing | `ipconfig`/`tailscale.exe` locations | Low | PR 1 |
| `mcode-tools` lookup | `media.py` `_CLI_CANDIDATES` (`/opt/homebrew/bin`, `/usr/local/bin`) | `PATH` and `~/.minimax/bin` work | `.exe` suffix and different dirs | Low | later |
| launchd jobs | `scripts/install-launchd.sh`, `scripts/uninstall-launchd.sh`, `bin/install_recap_cron.sh`, `bin/run_recap_cron.sh`, `scripts/poll_analytics.sh`, `bin/watchdog-server.sh` (uses `lsof`) | systemd user units or cron; developer tooling, not the app | Task Scheduler; not needed for the app | Low | out of scope |
| Developer helpers | `Makefile` (`open`, `lsof`), `scripts/ConductLoop.app.command` (`open -a`, `/Applications/*.app`), `desktop-demo/ConductLoop Demo Server.command` | `xdg-open`, `ss` | PowerShell equivalents | Low | out of scope |
| Release script | `scripts/release-desktop.sh` (`sed -i ''`, `codesign`, `notarytool`, `stapler`, `strings`, `nm`, `ditto`, `spctl`, `bundle_dmg.sh`, `tar --no-mac-metadata`) | **never ported; do not touch** | same | - | none |
| Release check | `scripts/release_check.py` (`EXPECTED_MIN_MACOS`, macOS manifest name), `tests/test_macos_minimum.py` | keep macOS-specific | same | Low | PR 11 adds checks, does not change these |
| Entitlements / signing | `desktop/src-tauri/entitlements.plist`, `tauri.conf.json` `bundle.macOS` | n/a | n/a | - | none |
| Claude Desktop config path | `cli.py` points at `~/Library/Application Support/Claude/claude_desktop_config.json` | `~/.config/Claude/...` (**untested**) | `%APPDATA%\Claude\...` (**untested**) | Low | PR 1 |
| `/Applications` or `/opt/homebrew` hardcoded paths | `server.py`, `media.py`, `sessions/sandbox.py` (`SENSITIVE_HOME_PATHS`: `Library/Keychains`, `Library/Application Support/ConductLoop`) | add Linux equivalents (2.2) | not applicable while sandbox is off | Low | PR 2 |
| User-facing copy: "this Mac", "este Mac", "macOS keychain", "llavero de macOS" | `onboarding.py`, `routes_*.py`, `web/connectors.js`, `web/connectors-guide.js`, `web/byok-banner.js`, `web/learning.html`, `web/welcome.js`, `README.md` | platform-aware strings from one `/api` platform descriptor | same | Low but pervasive | PR 1 |
| Keyboard hints | `web/index.html` shows the command symbol | show Ctrl | show Ctrl | Low | PR 5 |
| Notifications | no native notifications are used (webhooks only, see `config.example.json`); no `osascript` | n/a | n/a | - | none |
| Spotlight, `open`, Finder reveal | none found in the app code | n/a | n/a | - | none |
| Fonts, window chrome | `tauri.conf.json` is macOS-oriented (`macOS` block, `shortDescription` "...for macOS") | untested | untested | Low | PR 4 / PR 11 |

Python dependencies in `requirements-core.txt` and `requirements.txt` have no
macOS-only packages. Wheels for `onnxruntime`, `fastembed`, `cryptography` and
`bcrypt` exist for Linux x86_64 and Windows x86_64; Linux/Windows arm64 was not
checked. Python 3.13+ is required (`pyproject.toml`).

---

## Plan de PRs

Small, ordered, Linux first and then the Windows base. This document is the
audit PR that precedes them (PR 0). Each later PR keeps the macOS behaviour
byte-for-byte (see "Invariantes de macOS"). "Tests" are what that PR must add.

**Linux (experimental)**

1. **Platform flags and honest copy.** One platform descriptor in the API
   (`platform`, `keychain`, `sandbox`, `browser`); `os_supported()` returns
   `false` off macOS with a distinct reason; replace "this Mac" copy; fix the
   data-dir comment and the README line; treat an empty `XDG_DATA_HOME` as unset.
   *Tests:* descriptor per platform with `sys.platform` monkeypatched; no "Mac"
   copy on non-darwin; `cargo test` for `os_supported()` per `cfg`; keep
   `test_macos_minimum.py` green.
2. **Sandbox hardening on Linux.** Hide `$XDG_RUNTIME_DIR`, `/run/dbus`,
   `/tmp/.X11-unix` and Linux keyring/data paths in `bwrap_argv()`; UI message
   when `bwrap` is missing or user namespaces are restricted. *Tests:* argv test;
   a real `bwrap` test (CI sets `CONDUCTLOOP_REQUIRE_SANDBOX=1`) that a Unix
   socket outside the sandbox is **not** reachable from inside.
3. **Linux keychain backend.** `secrets_store` selector with Secret Service,
   probe with timeout, allow-list of `keyring` backends, vault fallback; remove
   the literal `security` call in `connector_tokens`. Depends on PR 2. *Tests:*
   a fake backend; selector tests per platform; vault fallback when the probe
   fails; `OPS_DISABLE_KEYCHAIN` still disables everything.
4. **Linux packaging script** (new file, not `release-desktop.sh`): sidecar with
   the Linux triple, AppImage and `.deb`. *Tests:* the output name matches
   Tauri's `externalBin` rule; `shellcheck` if available.
5. **Linux menu and shortcuts.** Explicit File menu with Close Tab/Close Window;
   Help with Check for Updates...; platform-aware shortcut hints. *Tests:* a Rust
   unit test of the menu item ids per `cfg`; a JS test for the hint text.
6. **Linux CI bundle job** (`ubuntu-22.04`, artifacts with `retention-days: 3`).
   *Tests:* `test_workflows_guard.py` stays green (SHA pins, `timeout-minutes`).
7. **Linux updater manifest** (AppImage only, `.deb` gets the "use your package
   manager" message, `TargetsNotFound` handled in the UI). Never published as
   "latest" alone (7.3). *Tests:* manifest generator test; UI message test.

**Windows base**

8. **Portability fixes that are safe on every platform.** New `parent_watch`
   liveness (ctypes), `random_hex32` via `getrandom`, `SO_EXCLUSIVEADDRUSE` in
   `_bind_server()`, POSIX-only guards around `_limits`/`killpg`/`dir_fd`, a
   visible "code execution is disabled on this platform" state, the browser
   disabled, `PYTHONUTF8=1`, `.gitattributes`. *Tests:* each with a `win32`
   monkeypatch where possible; `tests/test_parent_watch.py` extended.
9. **`windows-latest` compile-only job** (Rust check and the Windows-safe Python
   subset, which also runs the real Windows tests of PR 8). *Tests:* the job
   itself; `test_workflows_guard.py`.
10. **Windows Credential Manager backend** (with the blob-size handling).
    *Tests:* selector and size-limit tests with a fake backend.
11. **Windows packaging (NSIS), Help menu entry and release-manifest design:** a
    new script, the `update-windows-x86_64.json` flow, and the "never publish a
    non-macOS release as latest" check in `scripts/release_check.py` (a check
    only). *Tests:* `release_check` tests.

Optional later: re-enable the in-app browser on Linux/Windows once the upstream
issues are resolved and the proxy is verified on real machines. A Windows
sandbox is a separate project and is **not** planned.

---

## Invariantes de macOS

These do not change in any PR of the plan:

- `scripts/release-desktop.sh` (signing, notarization, DMG, the macOS updater
  manifest and the publishing step).
- The `desktop-macos` job in `.github/workflows/desktop.yml` and the macOS
  behaviour of `ci.yml`.
- `tauri.conf.json` macOS settings: `bundle.targets` (`["dmg", "app"]`),
  `bundle.macOS` (including `minimumSystemVersion` 13.0 and entitlements),
  the updater endpoint `update-macos-aarch64.json` and the public key
  (`770EA4C292DC6BD2`).
- The macOS release assets and their names (`ConductLoop-<version>.dmg`,
  `ConductLoop-macos-aarch64.dmg`, `ConductLoop-<version>.app.tar.gz` and
  `.sig`, `update-macos-aarch64.json`) and the `releases/latest` channel.
- macOS keychain behaviour (`security` CLI, 4000-byte line limit, hex stdin),
  the `sandbox-exec` profile, the macOS 14 browser gate and the weak link of
  Network.framework. `tests/test_macos_minimum.py`, `tests/test_secrets_store_keychain.py`
  and `tests/test_tauri_capabilities.py` must stay green in every PR.
- No PR touches releases, tags or secrets, and none adds a self-hosted runner.

## What was not verified

Everything on Windows; the Linux in-app browser, proxy and Secret Service
paths; real AppImage/`.deb`/NSIS bundles; the updater on any platform other than
macOS; the Ubuntu 24.04 end-user sandbox experience; NixOS and Wayland. Only the
Python test-suite on Ubuntu (CI) and one local Linux `bwrap` probe were run.
