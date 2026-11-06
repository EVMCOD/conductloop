# In-app browser and the `browse` tool

ConductLoop's desktop app (macOS 14+) can open a visible Browser tab that an
agent drives with the same gates as a session you start by hand: new sites are
asked about, and click / type / select wait for your approval (`select` always
does). The agent never receives a screenshot — only a text summary of what it
read or did.

## What agents can call

Team agents (chat, scheduled triggers, Telegram) get a `browse` tool:

| Arg | Meaning |
|---|---|
| `task` (required) | One clear goal for the tab |
| `start_url` | Optional `http(s)` URL to open first |
| `mode` | `read` (default) or `act` |
| `title` | Short label on the Browser tab |

- **read:** `open`, `navigate`, `read`, `back`, `scroll`
- **act:** also `click`, `type`, `select` (every select and any other effect
  waits for your approval in the Browser tab)

On Linux and Windows the tool returns `platform_unsupported` and does not open
a session. Caps default to about 50 ¢ and 10 steps; `OPS_BROWSE_TIMEOUT`
(seconds, max 300) is the wall-clock limit for one delegated run. The session
stays in Browser if it times out, so you can take over.

## Related

- `ops_dashboard/browse_tools.py` — the tool
- `ops_dashboard/sessions/browser.py` / `browser_runner.py` — the session
- `docs/PORTABILITY.md` §3 — platform status of the in-app browser
