# ConductLoop in 5 minutes (demo mode)

Try ConductLoop with fictional data — no keys, no vault, no network. Everything
below was run on a clean Linux box; on macOS it is the same.

> **Use a throwaway data directory.** Demo mode writes its fixture rows into
> whatever database the server is pointed at. `CONDUCTLOOP_DB_PATH` and
> `OPS_DATA_DIR` below keep it away from your real data; do not drop them.

## 1. Install (≈2 min)

You need Python 3.13+ (CI runs 3.14).

```bash
git clone https://github.com/EVMCOD/conductloop.git && cd conductloop
python3 -m venv .venv
.venv/bin/pip install -r requirements-core.txt     # lean: no PyTorch, enough for the demo
```

(`requirements.txt` adds the Anthropic/OpenAI SDKs and local embedding engines;
you need it later, not for this demo.)

## 2. Start the demo server

```bash
mkdir -p /tmp/conductloop-demo
export CONDUCTLOOP_DEMO=1
export CONDUCTLOOP_DB_PATH=/tmp/conductloop-demo/ops.db   # demo rows go here, not into your real DB
export OPS_DATA_DIR=/tmp/conductloop-demo                 # config, logs and saved chats go here too
export OPS_CONFIG_PATH=/tmp/conductloop-demo/config.json
.venv/bin/python -m ops_dashboard serve --port 7777
```

You should see `▸ DEMO MODE — fixture data loaded: {...}` and
`serving on http://127.0.0.1:7777/`. In demo mode the scheduler is never started,
so no agent runs by itself.

## 3. Look around (browser, ≈2 min)

Open <http://127.0.0.1:7777/>. The demo has two fictional projects, **Mosaic**
and **Verdant**, ~30 notes, a dozen monitored URLs, revenue, Sentry and
analytics history.

| Page | What to try |
|---|---|
| `/` | The dashboard: projects, live URLs, revenue, anomalies |
| `/brain` | Ask the Brain about the demo notes (with no key it answers with a mock provider that says so) |
| `/setup` | The readiness checklist — what is missing before real use |
| `/credentials` | Where API keys go: the OS keychain, or an encrypted file; values are never shown again |
| `/servers` | Which server (cloud, local, installed binary) answers chat, image, video, speech and search |
| `/chat`, `/agents`, `/connectors`, `/sessions` | The AI team, agents, connectors and agent sessions |

## 4. Same data from the terminal (≈1 min)

In a second terminal, with the same four `export` lines:

```bash
.venv/bin/python -m ops_dashboard status              # documents by source, live URLs
.venv/bin/python -m ops_dashboard projects            # Mosaic and Verdant
.venv/bin/python -m ops_dashboard find "second brain" # full-text search over the demo notes
.venv/bin/python -m ops_dashboard brain "What is PARA?"
.venv/bin/python -m ops_dashboard setup               # the same checklist as /setup
```

Or over HTTP: `curl -s http://127.0.0.1:7777/api/health`,
`/api/projects`, `/api/find?q=second+brain`.

## 5. Make it yours

Stop the server (Ctrl+C), then unset the demo variables (or open a new shell):

```bash
unset CONDUCTLOOP_DEMO CONDUCTLOOP_DB_PATH OPS_DATA_DIR OPS_CONFIG_PATH
rm -rf /tmp/conductloop-demo        # the demo is disposable
```

Next steps, in order:

1. `cp .env.example .env` and set `OPS_VAULT_ROOT` (your markdown vault),
   then `make index`.
2. Add a model key in **Credentials** (`/credentials`), or export
   `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `BRAIN_PROVIDER=ollama`.
3. `make serve` for the UI on :7777. For agents on a schedule, start it with
   `python -m ops_dashboard serve serve` instead; plain `serve` has no scheduler
   (see [AGENTS.md](../AGENTS.md#running)).

The macOS app (`.dmg`, macOS 13+) bundles all of this; see the
[README](../README.md#install).
