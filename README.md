# ConductLoop

> Local-first second brain and AI-agent cockpit. Python + SQLite backend, native macOS app (Tauri), bring-your-own-key models.

ConductLoop indexes your own material — an Obsidian vault, memory/notes folders, code repositories, a skills directory, project playbooks and a list of URLs you want to watch — into a single local SQLite database with full-text search (FTS5), fuzzy matching and optional local semantic embeddings. On top of that index it runs a **Brain** (RAG chat with citations) and a set of AI agents/teams (Boardroom, tasks, runs, scheduler) using the model provider *you* configure.

- **Website:** <https://conductloop.com>
- **macOS builds:** [EVMCOD/conductloop-releases](https://github.com/EVMCOD/conductloop-releases/releases/latest)
- **Price:** free. There is no license gate, subscription or trial in the app (`licensing.enforce()` returns `False`).

> The Python package is still named `ops_dashboard` for backwards compatibility. The product name is **ConductLoop**.

---

## Principles

### Local-first
- The server is a stdlib `http.server` that binds to `127.0.0.1:7777` by default.
- All indexed content, memory, tasks and settings live in a local SQLite file (`ops.db`). Location, in order of precedence:
  1. `$OPS_DATA_DIR/ops.db`
  2. `<repo>/data/ops.db` (dev checkout, if `data/` exists)
  3. macOS `~/Library/Application Support/com.conductloop.desktop/data/ops.db` · Linux `~/.local/share/conductloop/ops.db` · Windows `%LOCALAPPDATA%\ConductLoop\data\ops.db`
- At-rest secrets (DB encryption key, connector credentials) go to the macOS Keychain, or to `0600` files in the data dir elsewhere (`ops_dashboard/secrets_store.py`). Connector credentials are Fernet-encrypted in the DB.
- Network calls only happen for features you turn on: your model provider, connectors you configure (GitHub, Stripe, Slack, Telegram, Discord, Google Search Console/Analytics, Sentry, …), URL health checks, webhooks, and the Tauri auto-updater in the desktop app.
- Error reporting to Sentry is **opt-in** (only if you set `SENTRY_DSN`).
- If you install `sentence-transformers`, the embedding model (`all-MiniLM-L6-v2`) is downloaded once from Hugging Face and then runs locally.

### BYOK (bring your own key)
The Brain talks directly to the provider you configure; nothing is proxied through ConductLoop servers.

| Provider (`BRAIN_PROVIDER`) | Needs |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `minimax` | `MINIMAX_API_KEY` (OpenAI-compatible; `MINIMAX_BASE_URL` optional) |
| `gateway` | `GATEWAY_API_KEY` for an OpenAI-compatible gateway (`GATEWAY_BASE_URL`, default Vercel AI Gateway) |
| `ollama` | a local Ollama on `localhost:11434` (`OLLAMA_MODEL`, `OLLAMA_BASE_URL` optional) |
| `mock` | nothing — offline/testing |

`BRAIN_PROVIDER=auto` (default) picks the first available provider; `BRAIN_MODEL` overrides the model. Keys can also be set from the UI, which stores them in your local `config.json` (`~/ops-dashboard/config.json`, or `$OPS_CONFIG_PATH`). That file is gitignored — never commit it.

---

## Install

### macOS app
Download the latest `.dmg` from [conductloop-releases](https://github.com/EVMCOD/conductloop-releases/releases/latest), drag ConductLoop to `/Applications` and open it. The app is a Tauri v2 shell that starts the bundled Python server as a sidecar and shows it in a native WebView.

### From source (macOS / Linux)
Requires Python **3.13+** (CI runs 3.14).

```bash
git clone https://github.com/EVMCOD/conductloop.git
cd conductloop
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
make serve            # → http://127.0.0.1:7777
```

Then, optionally, point it at your content and index it:

```bash
cp .env.example .env  # the make targets load .env automatically
make index            # index all configured sources
```

Dependency sets:
- `requirements-core.txt` — lean runtime (server, CLI, auth, search). No ML.
- `requirements.txt` — core + Anthropic/OpenAI SDKs + `sentence-transformers` for local semantic search (pulls PyTorch, ~2 GB).
- `requirements-optional.txt` — Whisper voice notes, Google Analytics, PyJWT, Sentry, MCP server.
- `requirements-dev.txt` — core + pytest, ruff, mypy, pip-audit, pre-commit.

`pip install -e .` also installs the `ops` CLI entry point.

### Configuration
A fresh install is empty: no projects, teams or URLs. Point it at your content with environment variables (see `.env.example`), e.g. `OPS_VAULT_ROOT`, `OPS_WORKSPACE`, `OPS_SKILLS_DIR`, `OPS_REPOS`, `OPS_PROJECTS_FILE`, `OPS_LIVE_URLS_FILE`. Set `CONDUCTLOOP_DEMO=1` to load fictional demo data instead.

Optional notification webhooks (Slack / Discord / Telegram) are described in `config.example.json`. Remote access from a phone is documented in [`docs/REMOTE.md`](docs/REMOTE.md) (Tailscale; nothing is exposed publicly by default).

---

## Usage

```bash
make help        # list targets
make index       # reindex everything
make serve       # start the web UI on :7777
make brain       # ask the Brain from the terminal
make briefing    # morning briefing
make snapshot    # take a daily snapshot
make diff        # diff today vs yesterday
```

Selected `ops` / `python -m ops_dashboard` commands: `index`, `find`, `brain`, `briefing`, `capture`, `todos`, `projects`, `playbook`, `snapshot`, `backup`, `export` / `import`, `sync-export` / `sync-import` (passphrase-encrypted bundle), `obsidian-sync`, `calendar-import`, `mcp` (expose the Brain over MCP), `setup` (readiness checklist). Run `ops --help` for the full list.

---

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install                  # optional: ruff + basic hygiene hooks

ruff check ops_dashboard tests      # lint (blocking in CI)
mypy ops_dashboard                  # type check (advisory in CI; a strict subset is blocking)
pytest -m "not live"                # hermetic test suite (what CI runs)
```

Tests marked `live` need the network or real credentials; they never run by default or in CI.

### Layout
```
ops_dashboard/          Python package: server.py (HTTP API + UI), cli.py (typer CLI),
                        db*.py (SQLite schema/queries), brain.py (RAG + providers),
                        indexers/ (vault, memory, repos, skills, urls, …), web/ (vanilla HTML/JS UI)
desktop/                Tauri v2 macOS app (Rust shell + PyInstaller sidecar build)
desktop-demo/           Tauri demo build
cloudflare-worker/      optional status-page Worker (not the license service)
scripts/, bin/          launchd jobs, release and maintenance scripts
tests/                  pytest suite
```

### Desktop app
See [`desktop/README.md`](desktop/README.md) and [`desktop/docs/DISTRIBUTION.md`](desktop/docs/DISTRIBUTION.md). Building and signing a release needs your own Apple Developer ID and your own Tauri updater key; neither is in this repository.

---

## Contributing & security
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md) — please report vulnerabilities privately, not in public issues.

## License
[MIT](LICENSE). The ConductLoop name and logo are not covered by the license.
