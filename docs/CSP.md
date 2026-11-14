# Content Security Policy

ConductLoop has **two** policies. They govern different pages, which is easy to
miss because the desktop app shows one window.

| Policy | Where it lives | What it governs |
|---|---|---|
| `app.security.csp` | `desktop/src-tauri/tauri.conf.json` | The assets Tauri serves itself: `desktop/src-tauri/loading/index.html`, the page shown while the sidecar starts. |
| `LEGACY_PAGE_CSP` and the strict `PAGE_CSP` | `ops_dashboard/server.py`, `routes_sessions.py`, `routes_connectors.py`, `routes_learning.py` | The dashboard itself. After start-up the window navigates to `http://localhost:7777` (the bundled Python server), so the WebView applies **the server's `Content-Security-Policy` header**, not the one in `tauri.conf.json`. |

Until now `tauri.conf.json` had `"csp": null` and the server sent only
`object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`
on the legacy pages, so scripts, styles, images and `fetch` targets were not
restricted at all on most of the UI.

## What the UI really loads (inventory)

Scanned in `ops_dashboard/web/` (HTML, JS, CSS), the loading page and the
Python-built pages:

| Kind | Finding | Directive |
|---|---|---|
| Backend origin | Everything is same-origin and relative (`/api/...`). The window uses `http://localhost:7777`; `make serve` uses `127.0.0.1:7777`. | `'self'` |
| Scripts | All `<script src="/...js">` are same-origin. No CDN, no `eval`, no `new Function`, no string `setTimeout`. | `script-src 'self'` |
| Inline scripts | 21 legacy pages have one inline `<script>` block each. | `'unsafe-inline'` (legacy only) |
| Event handlers | `onclick=`/`onchange=` in `index.html` (9) and inside HTML strings built by `app.js` (about 20). | `'unsafe-inline'` (legacy only) |
| Styles | `<style>` blocks in 21 pages, `style=` attributes (about 1,000, almost all in `app.js` strings and `index.html`). `@import url('/theme.css')` is same-origin. | `'unsafe-inline'` (legacy only) |
| Fonts | System font stacks only; no `@font-face`, no web fonts. | `font-src 'self'` |
| Images | Same-origin (`/api/pair/qr.svg`, `/api/media/file/<id>`), `data:` SVG (`apple-touch-icon`, manifest icons). | `img-src 'self' data: blob:` |
| Media | Same-origin generated assets, `blob:` for the voice-note recorder (`URL.createObjectURL`). | `media-src 'self' blob:` |
| `fetch`, `EventSource` | Same-origin only (`/api/...`, SSE streams `/api/tasks/<id>/stream`, `/api/agents/<id>/stream`, `/api/boardroom/ceo/<id>/events`). No WebSocket, no beacons. | `connect-src 'self'` |
| BYOK providers | **Not called by the frontend.** Keys are sent to the local server, which calls the provider (Python). No provider origin is needed in `connect-src`. | none |
| Frames | None (`X-Frame-Options: DENY`, no `<iframe>`). In-app browser tabs are separate native WebViews, not frames. | `frame-src 'none'` |
| Service worker | Retired (`sw.js` only unregisters itself). | none |
| Tauri IPC | `browser.html` and `app.js` use `window.__TAURI_INTERNALS__.invoke`, which uses `ipc:` (macOS/Linux) or `http://ipc.localhost` (Windows). | `connect-src ... ipc: http://ipc.localhost` |
| Updater | Runs in Rust (`tauri-plugin-updater`); no WebView request. | none |
| External links | `<a href="https://...">` and `window.open` (docs, provider consoles, note links). Navigation is not governed by CSP. | none |

### The policies

`tauri.conf.json` (loading page, strictest possible):

```
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
font-src 'self'; connect-src ipc: http://ipc.localhost; frame-src 'none';
object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

Tauri 2 appends hashes (local scripts) and a nonce (`<style>` elements) to
`script-src`/`style-src` at build time, so the page's `<style>` block works
without `'unsafe-inline'`. It does **not** cover `style=""` attributes, which is
why the loading page uses a class. `dangerousDisableAssetCspModification` must
stay unset.

`LEGACY_PAGE_CSP` (all dashboard pages that still have inline code):

```
default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self';
connect-src 'self' ipc: http://ipc.localhost; manifest-src 'self'; frame-src 'none';
object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
```

`'unsafe-inline'` is there **only** because of the inline code listed above; it is
not in `default-src`, there is no `'unsafe-eval'`, and no remote origin is
allowed anywhere. The strict `PAGE_CSP` (sessions, connectors, learning, chat)
is unchanged: no inline code at all.

Behaviour changes to be aware of:

* A remote image in a note (`![](https://...)` rendered by `renderMarkdown`) no
  longer loads. That is intended: it closes a tracking-pixel / exfiltration
  channel for content that agents or imported notes can write.
* The voice-notes list builds `<audio src="file://...">`. A page served over
  `http://` cannot load `file:` URLs in any browser, so this never worked; it is
  recorded as a known gap in `tests/test_csp_static.py` (fix: serve the file
  through a local route).

## What to refactor to drop `'unsafe-inline'`

In order of effort. `tests/test_csp_static.py` holds the current counts
(`LEGACY_INLINE`) and fails if any grow; lower them as you go.

1. **Inline `<script>` blocks** (21 pages): move each to `/<page>.js`, add
   `defer`. Pages already on the strict policy show the pattern
   (`sessions.html` + `sessions.js`). Removes `'unsafe-inline'` from `script-src`
   once handlers are gone too.
2. **`onclick=` / `onchange=`** in `index.html` and `app.js`: replace with
   `data-action="..."` plus one delegated `addEventListener` per page.
   Functions called from handlers (`zoomBy`, `openDocEditor`, `askBrain`, ...)
   are globals today; delegation removes that coupling.
3. **`style=` attributes and `<style>` blocks**: move to CSS classes in
   `theme.css` / `app.css`; dynamic values go through CSSOM (`el.style.x = ...`),
   which CSP allows. This is the biggest item (about 1,000 occurrences in
   `app.js`) and the least valuable: style injection is far less dangerous than
   script injection, so `style-src 'unsafe-inline'` can be the last thing to go.
4. When the counts reach zero the test **requires** removing the matching
   `'unsafe-inline'`, and the page can move to the strict `PAGE_CSP`.

## Checks

* CI (`tests/test_csp_static.py`, no Mac needed): `tauri.conf.json` parses and
  its CSP is not null; both policies have the minimum directives, no wildcard,
  no scheme-wide source, no `unsafe-eval`; the Tauri IPC sources are present;
  no UI file loads a resource from outside the local server; every inline
  script/handler/style is covered by the policy of the page that has it; the
  server sends the expected header.
* Not covered by CI (needs the Mac, WKWebView): see the checklist in issue #32
  ("CSP" section): every screen opens with no violation in the WebView console,
  the updater, external links, IPC.
