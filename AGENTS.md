# Agents — 24/7 ops team

The agents layer lets you build a persistent, multi-level organization of AI 
agents that can run on schedules, log what they do, and remember things across 
sessions.

## Data model

| Table | What |
|-------|------|
| `team_members` | Agents (existing, extended with `level` + `manager_id`) |
| `agent_memory` | Persistent notes per agent, ranked by importance |
| `agent_triggers` | Cron / webhook / event rules that wake an agent |
| `agent_logs` | One row per "thing the agent did" — wakes, chats, errors |
| `tasks` / `runs` | Optional long-running work assigned to an agent |

## Architecture

```
        ┌─────────────┐
        │  Scheduler  │  ticks every 30s
        └──────┬──────┘
               │ polls agent_triggers
               │ matches cron expr
               ▼
        ┌─────────────┐         ┌─────────────┐
        │   _wake_    │ ──────▶ │   brain.ask │  uses agent's
        │   agent     │         │  (provider) │  memory + chain
        └──────┬──────┘         └─────────────┘
               │
               ▼
        ┌─────────────┐
        │  agent_logs │  wake, run_complete, error, memory_add
        └─────────────┘
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/agents/activity` | Recent activity across all agents |
| `GET`  | `/api/agents/{id}` | Agent detail (alias of `/api/members/{id}`) |
| `GET`  | `/api/agents/{id}/memory?q=` | List memories (optional search) |
| `POST` | `/api/agents/{id}/memory` | Add memory `{content, tags, importance}` |
| `PUT`  | `/api/agents/{id}/memory/{mid}` | Update memory |
| `DELETE` | `/api/agents/{id}/memory/{mid}` | Delete memory |
| `GET`  | `/api/agents/{id}/triggers` | List triggers for agent |
| `POST` | `/api/agents/{id}/triggers` | Add trigger `{kind, spec, prompt, enabled}` |
| `PUT`  | `/api/agents/{id}/triggers/{tid}` | Update trigger (toggle enabled, edit prompt) |
| `DELETE` | `/api/agents/{id}/triggers/{tid}` | Delete trigger |
| `GET`  | `/api/agents/{id}/logs?limit=&action=` | List activity for one agent |
| `POST` | `/api/agents/{id}/wake` | Manually wake agent with `{prompt}` |
| `GET`  | `/api/agents/{id}/stream` | SSE stream of new log entries for one agent |
| `GET`  | `/api/agents/activity/stream` | SSE stream of activity across all agents |
| `POST` | `/api/webhook/{token}` | External systems wake an agent via HTTP. Token identifies a webhook-kind trigger. Body appended to prompt. |
| `GET`  | `/api/approvals` | List pending approval items (proposed writes) |
| `POST` | `/api/approvals/{id}/approve` | Approve a pending action |
| `POST` | `/api/approvals/{id}/reject` | Reject a pending action |

## Cron syntax

Standard 5-field cron. Supports `*`, `N`, `N-M`, `N,M`, `*/N`, day-of-week 
(`MON`/`TUE`/...). Examples:

| Spec | Meaning |
|------|---------|
| `*/5 * * * *` | Every 5 minutes |
| `0 9 * * *` | Every day at 09:00 |
| `0 9 * * MON` | Every Monday at 09:00 |
| `*/30 9-17 * * *` | Every 30min during work hours |

## Trigger flow

1. Scheduler tick (every 30 s; `--scheduler-interval` on `ops serve serve`).
2. For each enabled trigger with `kind='cron'`, check if `spec` matches current time.
3. If match and not fired this minute, call `scheduler._wake_agent()`.
4. Wake logs to `agent_logs` (action='wake'), resolves provider from 
   `agent.model` (`provider:model` syntax), injects top-15 memories by 
   importance + reporting chain into the system prompt, calls `brain.ask`.
5. Response logged as `run_complete`. Trigger marked fired.

## Running

The scheduler is a thread of the server process, and **where it runs depends on
how you start the server**:

| How you start it | Scheduler |
|---|---|
| The desktop app (the bundled sidecar, `desktop/server_entry.py`) | yes, every 30 s (not in demo mode) |
| `python -m ops_dashboard serve serve` (the `serve` subcommand of `serve`) | yes, by default |
| `ops serve` / `python -m ops_dashboard serve` / `make serve` | **no**: only `--host` and `--port` exist, so agents only run when woken manually |

`ops` is the console script installed by `pip install -e .`; it is the same as
`python -m ops_dashboard`. The double `serve` is not a typo: `serve` is a
command group whose default action has no scheduler options, and its `serve`
subcommand adds them.

```bash
# Server with the scheduler (default tick: 30 s)
ops serve serve --port 7777

# Custom tick
ops serve serve --port 7777 --scheduler-interval 10

# Explicitly without scheduler (manual wake only; same as plain `ops serve`)
ops serve serve --port 7777 --no-scheduler
```

In demo mode (`CONDUCTLOOP_DEMO=1`, see [docs/FIRST-5-MINUTES.md](docs/FIRST-5-MINUTES.md))
the scheduler is never started.

## System jobs

Besides agent triggers, every scheduler tick also runs the app's own jobs. Each
has a cron expression that can be overridden with an environment variable
(`scheduler._system_jobs`):

| Job | Default (local time) | Variable |
|---|---|---|
| `learning_maintenance` | `15 4 * * *` | `LEARNING_MAINTENANCE_CRON` |
| `briefing` | `0 8 * * *` | `BRIEFING_CRON` |
| `obsidian_export` | `0 3 * * *` | `OBSIDIAN_EXPORT_CRON` |
| `analytics_poll` | `30 7 * * *` | `ANALYTICS_POLL_CRON` |
| `library_index` | `17 * * * *` | `LIBRARY_INDEX_CRON` |
| `daily_note` | `40 8 * * *` | `DAILY_NOTE_CRON` |

## Adding a trigger from CLI

There's no CLI for triggers yet; use the UI or curl:

```bash
curl -X POST http://localhost:7777/api/agents/100/triggers \
  -H 'Content-Type: application/json' \
  -d '{"kind":"cron","spec":"*/15 * * * *","prompt":"check example.com status"}'
```

## Agent API

| Endpoint | What |
|---|---|
| `POST /api/agents/create` | Create an agent |
| `POST /api/agents/run` | Run an agent once: `{"id", "prompt"?}` (local only) |
| `GET /api/agents/activity` (+ `/stream`, SSE) | Recent activity of all agents |
| `GET`/`POST /api/agents/<id>/memory` | List / add memories (per-item actions under `/memory/<mem_id>`) |
| `GET`/`POST /api/agents/<id>/triggers` | List / add triggers (per-item actions under `/triggers/<trigger_id>`) |
| `GET /api/agents/<id>/logs`, `/stream`, `/tools` | The agent's log, its live stream and its tools |
| `POST /api/agents/<id>/wake` | Wake it now |

## Memory injection

When an agent is woken (manually or by trigger), `top_agent_memory()` returns 
the top-15 memories by `importance` and they're injected in the system prompt:

```
## Your memory (top items, by importance)
- (9/10 [preference]) user prefers concise responses
- (8/10) example.com launched 2026-06-19
- (5/10) project budget is €0
```

Agents can add memories via the API or UI; the scheduler logs every `memory_add`
so you can audit what each agent is learning.
