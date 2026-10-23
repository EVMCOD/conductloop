"""SQLite + FTS5 schema and helpers for ops-dashboard."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("conductloop.db")


def vault_root() -> Path:
    """Root of the knowledge vault to index. Identity-neutral default.

    Override per-deployment with OPS_VAULT_ROOT. The default is a generic
    location so a fresh install indexes nothing personal until the user
    points it at their own vault.
    """
    return Path(
        os.environ.get("OPS_VAULT_ROOT", str(Path.home() / "Documents" / "vault"))
    ).expanduser()

# Audit 2026-06-23: moved imports to top-level so missing deps fail loud
# at module load instead of silently returning False on auth calls.
try:
    import bcrypt  # type: ignore
    _HAS_BCRYPT = True
except ImportError:
    bcrypt = None  # type: ignore
    _HAS_BCRYPT = False

try:
    from cryptography.fernet import Fernet  # type: ignore
    _HAS_FERNET = True
except ImportError:
    Fernet = None  # type: ignore
    _HAS_FERNET = False

# Resolve the on-disk DB path. Three-tier lookup so the sidecar still
# works whether launched by `python -m ops_dashboard` (dev), by the .app
# bundle (Tauri ships the sidecar as a Resources/sidecar/... pyinstaller
# --onedir folder that can't write next to __file__), or by future
# unpacked binaries on Linux/Windows.
#   1. $OPS_DATA_DIR / db file          — explicit env override
#   2. <repo>/data/ops.db               — backwards-compat: dev mode
#   3. macOS: ~/Library/Application Support/com.conductloop.desktop/data/ops.db
#      Linux: ~/.local/share/conductloop/ops.db
#      Win:   %LOCALAPPDATA%/ConductLoop/data/ops.db
def _default_data_dir() -> Path:
    from .app_paths import data_dir  # single source of truth (config, backups… use it too)
    return data_dir()

_DB_DIR = _default_data_dir()
_DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DB_DIR / "ops.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,           -- obsidian | memory | repo | live_url
    path        TEXT    NOT NULL,
    title       TEXT,
    content     TEXT    NOT NULL,
    metadata    TEXT,                       -- JSON blob
    mtime       INTEGER,
    size        INTEGER,
    project_key TEXT,                       -- which business this document belongs to
    indexed_at  INTEGER NOT NULL,
    UNIQUE(source, path)
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_mtime  ON documents(mtime DESC);
CREATE INDEX IF NOT EXISTS idx_documents_project_key ON documents(project_key);

-- documents_fts (FTS5 index), its content view and sync triggers are created
-- by db_fts.ensure_documents_fts(): trigger bodies contain ';' and can't go
-- through the ';'-split statement loop in init_db().

CREATE TABLE IF NOT EXISTS index_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    indexed     INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    notes       TEXT
);

-- Per-project playbook state. One row per (project, phase).
CREATE TABLE IF NOT EXISTS playbook_phases (
    project_key  TEXT NOT NULL,
    phase        TEXT NOT NULL,           -- 'Co-Pilots' | 'Brains' | 'Agents' | 'Hibernating'
    notes        TEXT,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (project_key)
);

-- Free-form playbook entries (extracted from playbook/<key>.md).
CREATE TABLE IF NOT EXISTS playbook_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key  TEXT NOT NULL,
    entry_kind   TEXT NOT NULL,           -- 'agent_in_use' | 'proposed_agent' | 'risk' | 'blocker' | 'win' | 'note' | 'cross_ref'
    content      TEXT NOT NULL,
    done         INTEGER NOT NULL DEFAULT 0,   -- parsed from `- [x]` / `- [ ]`
    order_idx    INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playbook_entries_project ON playbook_entries(project_key, order_idx);
CREATE UNIQUE INDEX IF NOT EXISTS uq_playbook_entries_project_content ON playbook_entries(project_key, content);

-- Cross-source TODO/pendiente tracker. One row per checklist item / TODO line.
CREATE TABLE IF NOT EXISTS todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT,                                -- may be NULL if no project match
    source        TEXT NOT NULL,                       -- obsidian | memory | repo | playbook
    doc_id        INTEGER,                             -- FK to documents.id (NULL for playbook)
    content       TEXT NOT NULL,
    done          INTEGER NOT NULL DEFAULT 0,
    mtime         INTEGER,
    extracted_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project_key);
CREATE INDEX IF NOT EXISTS idx_todos_done    ON todos(done);

-- User-created todos (separate from auto-extracted)
CREATE TABLE IF NOT EXISTS user_todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT    NOT NULL,
    project_key   TEXT,
    tags          TEXT,                       -- JSON array
    status        TEXT    NOT NULL DEFAULT 'open',  -- open | done | archived
    priority      INTEGER NOT NULL DEFAULT 0,       -- 0 = normal, 1 = high
    due_date      INTEGER,                    -- unix timestamp (start of day)
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    completed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_user_todos_status  ON user_todos(status);
CREATE INDEX IF NOT EXISTS idx_user_todos_project ON user_todos(project_key, status);
CREATE INDEX IF NOT EXISTS idx_user_todos_due     ON user_todos(due_date);

-- Recent activity feed
CREATE TABLE IF NOT EXISTS activity_feed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,           -- todo_added | todo_done | idea_added | idea_done | skill_used | note_created | url_failed | url_recovered | brain_ask
    project_key TEXT,
    summary     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_feed_created ON activity_feed(created_at DESC);

-- Teams: groups of humans + AI agents working on a project/area
CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    type        TEXT NOT NULL DEFAULT 'mixed',   -- human_team | ai_squad | mixed
    color       TEXT,
    icon        TEXT,                              -- emoji or short text
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_teams_type ON teams(type);

CREATE TABLE IF NOT EXISTS team_members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'human',   -- human | ai_agent | system
    role          TEXT,                             -- founder | lead | dev | designer | marketer | agent | observer
    status        TEXT NOT NULL DEFAULT 'active',   -- active | paused | retired
    contact       TEXT,                             -- email, slack, etc for humans
    capabilities  TEXT,                             -- for AI: what they can do
    model         TEXT,                             -- for AI: ollama:llama3.2, anthropic:claude, etc
    notes         TEXT,
    workload      INTEGER DEFAULT 0,                -- open todos assigned (auto-computed)
    level         TEXT DEFAULT 'agent',             -- ceo | vp | director | manager | agent | observer
    manager_id    INTEGER,                          -- FK team_members.id (reports to); NULL = top of tree
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_team_members_team ON team_members(team_id);
CREATE INDEX IF NOT EXISTS idx_team_members_type ON team_members(type, status);

-- Skills library (imported from Obsidian / ECC)
CREATE TABLE IF NOT EXISTS skills (
    key          TEXT PRIMARY KEY,           -- 'ios-icon-gen', 'customer-billing-ops', ...
    name         TEXT NOT NULL,
    description  TEXT NOT NULL,
    category     TEXT NOT NULL,              -- 'frontend' | 'backend' | 'devops' | 'ai' | ...
    subcategory  TEXT,                       -- 'iOS' | 'Python' | 'Stripe' | ...
    origin       TEXT,                       -- 'ECC' | 'community' | ...
    path         TEXT NOT NULL,              -- absolute path to SKILL.md
    mtime        INTEGER,
    content_len  INTEGER DEFAULT 0,          -- bytes of SKILL.md
    indexed_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_subcategory ON skills(subcategory);

-- Free-form ideas (quick capture, not todos)
CREATE TABLE IF NOT EXISTS ideas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT    NOT NULL,
    project_key   TEXT,
    tags          TEXT,                       -- JSON array
    status        TEXT    NOT NULL DEFAULT 'new',   -- new | in_progress | done | archived
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ideas_status  ON ideas(status);
CREATE INDEX IF NOT EXISTS idx_ideas_project ON ideas(project_key, status);

-- Brain conversation history (RAG chat)
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL,
    role             TEXT NOT NULL,           -- 'user' | 'assistant' | 'system'
    content          TEXT NOT NULL,
    citations        TEXT,                    -- JSON array of {source, path, title, score}
    provider         TEXT,
    model            TEXT,
    created_at       INTEGER NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

-- Daily snapshots for diffs (one row per day, summary of the dashboard at that moment)
CREATE TABLE IF NOT EXISTS snapshots (
    day           TEXT    NOT NULL,           -- YYYY-MM-DD
    kind          TEXT    NOT NULL,           -- 'auto' | 'weekly'
    taken_at      INTEGER NOT NULL,
    stats         TEXT    NOT NULL,           -- JSON: total docs, by source
    url_status    TEXT    NOT NULL,           -- JSON: list of {name, ok, status_code, latency_ms}
    projects      TEXT    NOT NULL,           -- JSON: per-project {key, name, repos, last_activity, all_urls_ok, mention_count}
    todos         TEXT    NOT NULL,           -- JSON: by_project counts
    PRIMARY KEY (day, kind)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_day ON snapshots(day DESC);

-- Persistent brain preferences
CREATE TABLE IF NOT EXISTS brain_memory (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Free-form tags applied to documents (cross-cutting topics)
CREATE TABLE IF NOT EXISTS doc_tags (
    doc_id   INTEGER NOT NULL,
    tag      TEXT    NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (doc_id, tag),
    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON doc_tags(tag);

-- Tasks: units of work delegated to AI agents (or humans)
-- project_key is a soft reference (e.g. 'traincurve', 'rentcla', or NULL for general)
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key       TEXT,
    title             TEXT    NOT NULL,
    prompt            TEXT    NOT NULL,
    agent_member_id   INTEGER,                    -- references team_members.id (soft FK)
    provider          TEXT,                       -- 'anthropic' | 'openai' | 'ollama' | 'mock'
    model             TEXT,
    status            TEXT    NOT NULL DEFAULT 'queued',  -- queued | running | done | error | needs_approval
    output_summary    TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    created_by        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_key);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_member  ON tasks(agent_member_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

-- Runs: each attempt to execute a task (one task can have many runs via re-run)
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           INTEGER NOT NULL,
    provider          TEXT,
    model             TEXT,
    status            TEXT    NOT NULL DEFAULT 'running',  -- running | done | error | needs_approval
    artifact_doc_id   INTEGER,                    -- references documents.id (soft FK)
    output_path       TEXT,                       -- path of the .md written to disk
    output_text       TEXT,                       -- raw output of the agent
    tokens_in         INTEGER DEFAULT 0,
    tokens_out        INTEGER DEFAULT 0,
    duration_ms       INTEGER DEFAULT 0,
    tool_calls        TEXT,                       -- JSON: [{name, args, ok, ts}]
    error             TEXT,
    started_at        INTEGER NOT NULL,
    finished_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_task     ON runs(task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON runs(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_artifact ON runs(artifact_doc_id);

-- Code-file TODOs (auto-extracted from source files in repos)
CREATE TABLE IF NOT EXISTS code_todos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT,                                  -- may be NULL if no project match
    file_path     TEXT    NOT NULL,
    line          INTEGER NOT NULL,
    kind          TEXT    NOT NULL,                      -- todo | fixme | hack | xxx | bug | note
    priority      TEXT    NOT NULL DEFAULT 'normal',     -- urgent | high | normal | low
    content       TEXT    NOT NULL,
    done          INTEGER NOT NULL DEFAULT 0,
    extracted_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_todos_project  ON code_todos(project_key, priority);
CREATE INDEX IF NOT EXISTS idx_code_todos_priority ON code_todos(priority, done);
CREATE INDEX IF NOT EXISTS idx_code_todos_path     ON code_todos(file_path);

-- Decisions: free-form technical/strategic decisions with context
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT,
    title         TEXT    NOT NULL,
    context       TEXT,                                  -- why we were deciding
    decision      TEXT    NOT NULL,                      -- what we chose
    consequences  TEXT,                                  -- expected outcomes / risks
    links         TEXT,                                  -- JSON array of {label, url}
    tags          TEXT,                                  -- JSON array
    decided_at    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_key, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_date    ON decisions(decided_at DESC);

-- Costs: expenses per provider/category with monthly aggregation support
CREATE TABLE IF NOT EXISTS costs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key  TEXT,
    provider     TEXT    NOT NULL,                       -- 'cloudflare' | 'apple' | 'openai' | 'anthropic' | 'vercel' | 'render' | 'railway' | 'namecheap' | 'github' | 'cursor' | 'other'
    category     TEXT    NOT NULL DEFAULT 'infra',       -- 'infra' | 'api' | 'domain' | 'dev_tool' | 'subscription' | 'one_time'
    amount_cents INTEGER NOT NULL,                       -- store in cents to avoid float
    currency     TEXT    NOT NULL DEFAULT 'EUR',
    description  TEXT,
    period       TEXT    NOT NULL,                       -- YYYY-MM for recurring, YYYY-MM-DD for one-time
    billed_at    INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_period   ON costs(period, provider);
CREATE INDEX IF NOT EXISTS idx_costs_project  ON costs(project_key, period);
CREATE INDEX IF NOT EXISTS idx_costs_provider ON costs(provider, billed_at DESC);

-- Task templates: reusable checklists/workflows
CREATE TABLE IF NOT EXISTS task_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT    UNIQUE NOT NULL,               -- 'launch_checklist' | 'weekly_review' | 'incident_response' | 'new_project_setup'
    name          TEXT    NOT NULL,
    description   TEXT,
    icon          TEXT,                                  -- emoji
    items         TEXT    NOT NULL,                      -- JSON array of {title, description, optional: bool}
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

-- GitHub sync: PRs, issues, commits per repo
CREATE TABLE IF NOT EXISTS github_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full     TEXT    NOT NULL,                      -- 'EVMCOD/traincurve'
    item_type     TEXT    NOT NULL,                      -- 'pr' | 'issue' | 'commit'
    item_number   INTEGER,                               -- for PRs / issues
    title         TEXT,
    state         TEXT,                                  -- 'open' | 'closed' | 'merged'
    author        TEXT,
    url           TEXT    NOT NULL,
    created_at_remote INTEGER,                           -- from GitHub
    updated_at_remote INTEGER,
    fetched_at    INTEGER NOT NULL,
    UNIQUE(repo_full, item_type, item_number, url)
);
CREATE INDEX IF NOT EXISTS idx_github_items_repo   ON github_items(repo_full, item_type);
CREATE INDEX IF NOT EXISTS idx_github_items_state  ON github_items(state, updated_at_remote DESC);
CREATE INDEX IF NOT EXISTS idx_github_items_type   ON github_items(item_type, updated_at_remote DESC);

-- Voice notes: transcriptions of dictated notes (Whisper local)
CREATE TABLE IF NOT EXISTS voice_notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_path    TEXT,                                  -- original .webm / .wav on disk
    transcript    TEXT    NOT NULL,
    duration_sec  REAL,
    project_key   TEXT,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_notes_created ON voice_notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_notes_project ON voice_notes(project_key);

-- URL state changes: detected by live_urls indexer
CREATE TABLE IF NOT EXISTS url_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url_name      TEXT    NOT NULL,
    from_status   TEXT,                                  -- 'ok' | 'fail'
    to_status     TEXT    NOT NULL,
    status_code   INTEGER,
    latency_ms    INTEGER,
    changed_at    INTEGER NOT NULL,
    notified      INTEGER NOT NULL DEFAULT 0             -- whether webhooks fired
);
CREATE INDEX IF NOT EXISTS idx_url_changes_time   ON url_changes(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_url_changes_notified ON url_changes(notified, changed_at DESC);

-- Agent memory: per-agent persistent notes, injected in system prompt on chat
CREATE TABLE IF NOT EXISTS agent_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    tags        TEXT,                            -- JSON array of strings
    importance  INTEGER DEFAULT 5,               -- 1-10
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_memory_imp   ON agent_memory(agent_id, importance DESC);

-- Agent triggers: wake an agent on cron / webhook / event
CREATE TABLE IF NOT EXISTS agent_triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    spec          TEXT    NOT NULL,
    prompt        TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fired_at INTEGER,
    last_status   TEXT,
    last_error    TEXT,
    fire_count    INTEGER DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triggers_agent ON agent_triggers(agent_id, enabled);
CREATE INDEX IF NOT EXISTS idx_triggers_kind  ON agent_triggers(kind, enabled);

-- Agent activity log: one row per thing the agent did
CREATE TABLE IF NOT EXISTS agent_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER NOT NULL,
    trigger_id  INTEGER,                         -- which trigger woke it (NULL for manual)
    task_id     INTEGER,                         -- linked task if any
    action      TEXT    NOT NULL,                -- 'wake' | 'chat' | 'memory_add' | 'memory_use' | 'run_complete' | 'error'
    summary     TEXT,                            -- short description
    details     TEXT,                            -- JSON: full payload
    duration_ms INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_agent   ON agent_logs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_created ON agent_logs(created_at DESC);

-- External service connections (OAuth/API keys/MCP servers)
CREATE TABLE IF NOT EXISTS connections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT    NOT NULL,                -- 'cloudflare' | 'github' | 'notion' | 'linear' | 'stripe' | 'slack' | 'mcp' | 'openai' | 'anthropic' | 'minimax' | 'custom'
    name          TEXT    NOT NULL,                -- user-given label, e.g. 'production-cf'
    credentials   TEXT    NOT NULL,                -- JSON: {api_key, token, account_id, ...} (token stored in plain text - consider keychain in prod)
    config        TEXT,                            -- JSON: provider-specific config (zone, workspace, repo, etc.)
    status        TEXT    NOT NULL DEFAULT 'unknown',  -- 'ok' | 'fail' | 'unknown'
    last_used_at  INTEGER,
    last_check_at INTEGER,
    last_error    TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    UNIQUE(provider, name)
);
CREATE INDEX IF NOT EXISTS idx_connections_provider ON connections(provider, enabled);
CREATE INDEX IF NOT EXISTS idx_connections_status   ON connections(status);

-- MCP (Model Context Protocol) servers: each provides a set of tools to agents
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    UNIQUE NOT NULL,          -- 'playwright' | 'filesystem' | 'github-mcp' | ...
    transport     TEXT    NOT NULL DEFAULT 'stdio', -- 'stdio' | 'http' | 'sse'
    command       TEXT,                            -- for stdio: ['npx', '-y', '@modelcontextprotocol/server-playwright']
    url           TEXT,                            -- for http/sse
    env           TEXT,                            -- JSON: env vars
    enabled       INTEGER NOT NULL DEFAULT 1,
    tools_cache   TEXT,                            -- JSON: [{name, description, input_schema}]
    tools_cached_at INTEGER,
    last_error    TEXT,
    trust_readonly INTEGER NOT NULL DEFAULT 0,     -- the user trusts this server's read-only annotations
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled);

-- Agent \u2194 MCP tool bindings: which agents can use which MCP tools
CREATE TABLE IF NOT EXISTS agent_mcp_tools (
    agent_id      INTEGER NOT NULL,
    server_name   TEXT    NOT NULL,
    tool_name     TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (agent_id, server_name, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_agent_mcp_tools_agent ON agent_mcp_tools(agent_id);

-- ============================================================
-- Revenue / Billing (unified Stripe + Apple + Google + ...)
-- ============================================================

-- One row per connected revenue source (e.g. "stripe-rentcla", "apple-evtl")
CREATE TABLE IF NOT EXISTS revenue_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    UNIQUE NOT NULL,            -- e.g. 'stripe-rentcla', 'apple-evtl'
    source_type     TEXT    NOT NULL,                   -- 'stripe' | 'apple' | 'google' | 'mock'
    project_key     TEXT    NOT NULL,                   -- 'rentcla', 'evtl-tools', 'beibo', etc.
    connection_id   INTEGER,                            -- FK to connections (encrypted creds)
    currency        TEXT    NOT NULL DEFAULT 'USD',
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_poll_at    INTEGER,
    last_poll_status TEXT,                              -- 'ok' | 'error'
    last_poll_error TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_revenue_sources_project ON revenue_sources(project_key);
CREATE INDEX IF NOT EXISTS idx_revenue_sources_type ON revenue_sources(source_type);

-- One row per revenue event (charge, refund, subscription, dispute, etc.)
-- Idempotent via (source_type, external_id) UNIQUE.
CREATE TABLE IF NOT EXISTS revenue_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER,                            -- FK to revenue_sources (nullable for mock/test)
    source_type     TEXT    NOT NULL,                   -- denormalized for fast filter
    project_key     TEXT    NOT NULL,                   -- denormalized
    external_id     TEXT    NOT NULL,                   -- source's event ID (ch_xxx, txn_xxx, etc.)
    event_type      TEXT    NOT NULL,                   -- 'payment_succeeded' | 'subscription_created' | 'refund' | 'dispute_opened' | etc.
    amount_cents    INTEGER NOT NULL,                   -- positive for revenue, negative for refunds
    currency        TEXT    NOT NULL,
    customer_ref    TEXT,                               -- cus_xxx | App Store user | Google profile
    customer_email  TEXT,                               -- for display when source provides it
    product_ref     TEXT,                               -- plan/SKU ID
    product_name    TEXT,                               -- human-readable
    status          TEXT    NOT NULL DEFAULT 'succeeded', -- 'succeeded' | 'pending' | 'failed' | 'refunded'
    raw_payload     TEXT,                               -- JSON for debugging / replay
    occurred_at     INTEGER NOT NULL,                   -- source timestamp (epoch sec)
    ingested_at     INTEGER NOT NULL,                   -- when we stored it
    UNIQUE(source_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_revenue_events_project_time ON revenue_events(project_key, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_revenue_events_source_time ON revenue_events(source_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_revenue_events_type_time ON revenue_events(event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_revenue_events_occurred ON revenue_events(occurred_at DESC);

-- ============================================================
-- Google Analytics 4 — daily metrics per site/property
-- ============================================================

-- One row per registered GA4 property
CREATE TABLE IF NOT EXISTS ga_sites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id     TEXT    UNIQUE NOT NULL,            -- GA4 property ID ('123456789' or 'properties/123456789')
    display_name    TEXT    NOT NULL,                   -- human label, e.g. 'rentcla.com'
    url             TEXT,                               -- site URL for display / linking
    project_key     TEXT    NOT NULL,                   -- links to ops-dashboard project
    service_account_json TEXT,                          -- JSON content of the service account key (or OAuth authorized_user blob)
    source_type     TEXT,                               -- 'ga4' (real) | 'mock'; NULL = auto-detect by credentials
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_poll_at    INTEGER,
    last_poll_status TEXT,                              -- 'ok' | 'error'
    last_poll_error TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ga_sites_project ON ga_sites(project_key);
CREATE INDEX IF NOT EXISTS idx_ga_sites_enabled ON ga_sites(enabled);

-- One row per (site, date). Idempotent via UNIQUE(site_id, date).
CREATE TABLE IF NOT EXISTS ga_metrics_daily (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         INTEGER NOT NULL,                   -- FK to ga_sites
    property_id     TEXT    NOT NULL,                   -- denormalized
    project_key     TEXT    NOT NULL,                   -- denormalized
    date            TEXT    NOT NULL,                   -- YYYY-MM-DD
    users           INTEGER NOT NULL DEFAULT 0,
    new_users       INTEGER NOT NULL DEFAULT 0,
    sessions        INTEGER NOT NULL DEFAULT 0,
    page_views      INTEGER NOT NULL DEFAULT 0,
    events          INTEGER NOT NULL DEFAULT 0,
    engagement_rate REAL,                               -- 0.0-1.0
    avg_session_duration REAL,                         -- seconds
    bounce_rate     REAL,                               -- 0.0-1.0
    source_organic  INTEGER NOT NULL DEFAULT 0,
    source_direct   INTEGER NOT NULL DEFAULT 0,
    source_referral INTEGER NOT NULL DEFAULT 0,
    source_social   INTEGER NOT NULL DEFAULT 0,
    source_email    INTEGER NOT NULL DEFAULT 0,
    source_paid     INTEGER NOT NULL DEFAULT 0,
    top_country     TEXT,                               -- ISO country code with most users
    raw_payload     TEXT,                               -- JSON for debugging
    ingested_at     INTEGER NOT NULL,
    UNIQUE(site_id, date),
    FOREIGN KEY (site_id) REFERENCES ga_sites(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ga_metrics_site_date ON ga_metrics_daily(site_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_ga_metrics_project_date ON ga_metrics_daily(project_key, date DESC);
CREATE INDEX IF NOT EXISTS idx_ga_metrics_date ON ga_metrics_daily(date DESC);

-- ============================================================
-- Sentry — error tracking (per project)
-- ============================================================

-- One row per registered Sentry project
CREATE TABLE IF NOT EXISTS sentry_projects (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    UNIQUE NOT NULL,           -- e.g. 'sentry-rentcla'
    source_type         TEXT    NOT NULL DEFAULT 'sentry', -- 'sentry' | 'mock'
    project_key         TEXT    NOT NULL,                  -- 'rentcla', 'evtl-tools', etc.
    sentry_project      TEXT,                             -- Sentry project slug (URL component)
    org_slug            TEXT,                             -- Sentry org slug
    auth_token          TEXT,                             -- Bearer token (user-provided, no encryption in v1)
    enabled             INTEGER NOT NULL DEFAULT 1,
    last_poll_at        INTEGER,
    last_poll_status    TEXT,
    last_poll_error     TEXT,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sentry_projects_project ON sentry_projects(project_key);
CREATE INDEX IF NOT EXISTS idx_sentry_projects_enabled ON sentry_projects(enabled);

-- One row per Sentry issue. Idempotent via UNIQUE(project_id, issue_id).
CREATE TABLE IF NOT EXISTS sentry_issues (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL,                 -- FK to sentry_projects
    sentry_project_slug TEXT    NOT NULL,                 -- denormalized
    sentry_project_id   TEXT    NOT NULL,                 -- denormalized
    project_key         TEXT    NOT NULL,                 -- denormalized
    issue_id            TEXT    NOT NULL,                 -- Sentry's numeric issue ID (as string)
    title               TEXT    NOT NULL,
    culprit             TEXT,
    level               TEXT    NOT NULL DEFAULT 'error', -- 'error' | 'warning' | 'info' | 'fatal'
    status              TEXT    NOT NULL DEFAULT 'unresolved', -- 'unresolved' | 'resolved' | 'ignored'
    event_count         INTEGER NOT NULL DEFAULT 0,
    user_count          INTEGER NOT NULL DEFAULT 0,
    first_seen          INTEGER NOT NULL,
    last_seen           INTEGER NOT NULL,
    permalink           TEXT,
    raw_payload         TEXT,                             -- JSON for debugging
    updated_at          INTEGER NOT NULL,
    UNIQUE(project_id, issue_id),
    FOREIGN KEY (project_id) REFERENCES sentry_projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sentry_issues_project ON sentry_issues(project_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_sentry_issues_level_status ON sentry_issues(level, status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_sentry_issues_project_key ON sentry_issues(project_key, last_seen DESC);

-- ============================================================
-- Users + Auth + Billing (lifetime + Stripe-ready)
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT    UNIQUE NOT NULL,
    password_hash       TEXT,                           -- bcrypt hash (NULL for OAuth/magic-link only)
    name                TEXT,
    locale              TEXT    NOT NULL DEFAULT 'en',  -- preferred UI language
    is_admin            INTEGER NOT NULL DEFAULT 0,
    email_verified      INTEGER NOT NULL DEFAULT 0,
    verification_token  TEXT,
    created_at          INTEGER NOT NULL,
    last_login_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Active sessions (cookie-based auth). One row per browser session.
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,                    -- random 64-char hex
    user_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,                      -- 30 days default
    ip          TEXT,
    user_agent  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- Paired mobile/API devices (bearer-token auth). Created locally via the
-- pairing flow; the raw token is shown once (in the QR) and only its hash is
-- stored. Used so a phone can authenticate to the desktop server over LAN/relay.
CREATE TABLE IF NOT EXISTS device_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash    TEXT    UNIQUE NOT NULL,            -- sha256 of the raw bearer token
    name          TEXT,                                -- human label e.g. "iPhone"
    user_id       INTEGER,                             -- owner user (NULL in single-user local)
    created_at    INTEGER NOT NULL,
    last_seen_at  INTEGER,
    revoked       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_hash ON device_tokens(token_hash);

-- LLM usage metering (foundation for credits / subscription billing). One row
-- per gateway/provider call that reports token usage.
CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT    NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at DESC);

-- Subscription/plan: lifetime (one-time purchase) is the focus. Stripe
-- monthly subscriptions supported but optional.
CREATE TABLE IF NOT EXISTS entitlements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL UNIQUE,
    plan                TEXT    NOT NULL DEFAULT 'free', -- 'free' | 'lifetime' | 'pro_monthly' | 'team_monthly'
    source              TEXT,                            -- 'stripe_lifetime' | 'stripe_monthly' | 'admin_grant' | 'trial' | 'launch_promo'
    stripe_customer_id  TEXT,
    stripe_subscription_id TEXT,
    stripe_payment_intent_id TEXT,
    amount_cents        INTEGER,
    currency            TEXT    DEFAULT 'USD',
    activated_at        INTEGER,
    expires_at          INTEGER,                         -- NULL for lifetime (active forever)
    refunded_at         INTEGER,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entitlements_plan ON entitlements(plan);
CREATE INDEX IF NOT EXISTS idx_entitlements_stripe ON entitlements(stripe_customer_id);

-- Founding members: original supporters who paid at the early-bird $300 tier.
-- They keep their lifetime access at the price they paid; they are NOT counted
-- in the public sales counter (which resets to 0 after the pivot to local-first).
CREATE TABLE IF NOT EXISTS founders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL UNIQUE,
    original_plan       TEXT    NOT NULL,                -- 'lifetime' | 'team_lifetime' | 'small_business_lifetime'
    original_cents      INTEGER NOT NULL,                -- price they actually paid
    badge_color         TEXT    DEFAULT 'gold'
                        CHECK (badge_color IN ('gold','silver','bronze')),
    note                TEXT,                            -- optional founder note (why they believed)
    joined_at           INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_founders_user ON founders(user_id);

-- Payment audit log (every stripe webhook + manual grant)
CREATE TABLE IF NOT EXISTS payment_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER,
    kind                TEXT    NOT NULL,                -- 'lifetime_purchase' | 'monthly_subscription' | 'refund' | 'webhook_received'
    stripe_event_id     TEXT,                            -- idempotency key
    payload             TEXT,                            -- JSON for debugging
    created_at          INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_events_user ON payment_events(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_events_stripe ON payment_events(stripe_event_id);

-- ============================================================
-- Note links (backlinks detection between docs)
-- ============================================================

-- One row per (source_doc, target_doc, link_type) pair.
-- source_doc = the doc that mentions/refers to target_doc
-- target_doc = the doc being referred to (resolved from title or path)
CREATE TABLE IF NOT EXISTS note_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_doc_id   INTEGER NOT NULL,
    target_doc_id   INTEGER NOT NULL,
    link_type       TEXT    NOT NULL DEFAULT 'mention',  -- 'mention' | 'tag' | 'wiki' | 'markdown'
    context         TEXT,                               -- snippet of the source text where the link appears
    created_at      INTEGER NOT NULL,
    UNIQUE(source_doc_id, target_doc_id, link_type),
    FOREIGN KEY (source_doc_id) REFERENCES documents(id) ON DELETE CASCADE,
    FOREIGN KEY (target_doc_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target_doc_id);
CREATE INDEX IF NOT EXISTS idx_note_links_source ON note_links(source_doc_id);

-- Boardroom (2026-07-01): chairman-only control panel for managing the AI C-Suite.
-- Reuses team_members for CEO rows (filtered by is_ceo=1) and adds 4 thin tables.
-- CEO messages and vault decisions are persisted here so the chairman can audit them.

CREATE TABLE IF NOT EXISTS autonomy_config (
    member_id        INTEGER PRIMARY KEY,
    default_tier     INTEGER NOT NULL DEFAULT 2,             -- 1 autonomous | 2 notify | 3 approve | 4 escalate
    maturity_stage   TEXT    NOT NULL DEFAULT 'traction',    -- chaotic | traction | scale | mature | pause
    per_action       TEXT    NOT NULL DEFAULT '{}',          -- JSON: {tool_name: tier_override, ...}
    require_chairman INTEGER NOT NULL DEFAULT 0,             -- 1 = manual approval for every action
    updated_at       INTEGER NOT NULL,
    FOREIGN KEY (member_id) REFERENCES team_members(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS boardroom_settings (
    id               INTEGER PRIMARY KEY CHECK (id = 1),    -- singleton
    chairman_user_id INTEGER NOT NULL,
    holding_ceo_id   INTEGER,                                -- the team_member that synthesizes the Daily Recap
    last_recap_at    INTEGER,
    updated_at       INTEGER NOT NULL,
    FOREIGN KEY (chairman_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (holding_ceo_id)   REFERENCES team_members(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ceo_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL,                            -- the CEO thread
    user_id     INTEGER,                                     -- chairman user_id (NULL for Holding CEO broadcasts)
    role        TEXT    NOT NULL,                            -- 'chairman' | 'ceo' | 'system'
    content     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (member_id) REFERENCES team_members(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)   REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_ceo_messages_member ON ceo_messages(member_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ceo_messages_user   ON ceo_messages(user_id);

CREATE TABLE IF NOT EXISTS boardroom_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,                            -- chairman who created/logged it
    member_id   INTEGER,                                     -- optional CEO this decision touches
    kind        TEXT    NOT NULL,                            -- 'ceo_created' | 'autonomy_changed' | 'tier_advanced' | 'note'
    title       TEXT    NOT NULL,
    body        TEXT,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (user_id)   REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (member_id) REFERENCES team_members(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_boardroom_decisions_user   ON boardroom_decisions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_boardroom_decisions_member ON boardroom_decisions(member_id, created_at DESC);

-- Boardroom chat-bindings (2026-07-01): telegram_chats / discord_channels.
-- One row per chairman-channel pair. Lets the cron push the daily recap
-- and lets inbound webhooks route replies to the right chat_id.
CREATE TABLE IF NOT EXISTS telegram_chats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,                  -- chairman user_id (soft FK)
    chat_id         INTEGER NOT NULL UNIQUE,  -- Telegram chat_id (from /start message)
    username        TEXT,                     -- last known @handle (best-effort)
    first_name      TEXT,
    bound           INTEGER NOT NULL DEFAULT 0,   -- 1 = approved by chairman via /api/telegram/chats-bind
    created_at      INTEGER NOT NULL,
    last_seen_at    INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_chats_user ON telegram_chats(user_id);
CREATE INDEX IF NOT EXISTS idx_telegram_chats_bound ON telegram_chats(bound);

CREATE TABLE IF NOT EXISTS discord_channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_url     TEXT NOT NULL,            -- discord channel webhook url
    label           TEXT,                     -- friendly name ('#boardroom-push')
    bound_by        INTEGER,                  -- chairman user_id who bound it
    created_at      INTEGER NOT NULL,
    last_used_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_discord_channels_bound ON discord_channels(bound_by);

-- Per-CEO channel bindings (added 2026-07-16).
-- Each row says: this CEO auto-publishes to this Discord channel / Telegram chat,
-- AND messages arriving on the bound chat are routed back as chairman messages
-- in the CEO's thread. 1:N per CEO, many CEOs can share a channel.
CREATE TABLE IF NOT EXISTS ceo_channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ceo_id          INTEGER NOT NULL,                 -- team_members.id (the AI CEO)
    platform        TEXT NOT NULL,                     -- 'telegram' | 'discord'
    target_id       TEXT NOT NULL,                     -- chat_id (telegram) or channel_id (discord) or webhook_url
    target_name     TEXT,                              -- display label ('#boardroom', '@john_doe')
    enabled         INTEGER NOT NULL DEFAULT 1,
    direction       TEXT NOT NULL DEFAULT 'both',      -- 'outbound' (CEO→channel only), 'inbound' (channel→CEO only), 'both'
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_posted_at  INTEGER,                           -- last successful outbound
    last_error      TEXT,                              -- last outbound error (if any)
    FOREIGN KEY (ceo_id) REFERENCES team_members(id) ON DELETE CASCADE,
    UNIQUE(ceo_id, platform, target_id)
);
CREATE INDEX IF NOT EXISTS idx_ceo_channels_ceo ON ceo_channels(ceo_id);
CREATE INDEX IF NOT EXISTS idx_ceo_channels_enabled ON ceo_channels(enabled) WHERE enabled = 1;

-- Daily Recap cache (2026-07-01): Holding CEO synthesis, TTL 6h.
-- Stores the LLM-generated brief so we don't call the API on every dashboard load.
CREATE TABLE IF NOT EXISTS recap_cache (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL DEFAULT 'daily',          -- 'daily' | 'intake' | ...
    payload_hash  TEXT    NOT NULL,                          -- SHA-256 of the input context (to invalidate on data drift)
    payload       TEXT    NOT NULL,                          -- JSON of the input (ceos, recent_messages, projects, kpis)
    synthesis     TEXT    NOT NULL,                          -- Markdown / prose brief from the Holding CEO
    source        TEXT    NOT NULL DEFAULT 'llm',            -- 'llm' | 'static' (fallback when no BYOK provider)
    provider      TEXT,
    model         TEXT,
    generated_at  INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recap_cache_lookup ON recap_cache(kind, payload_hash, expires_at);

-- ── Media (2026-10-23) ─────────────────────────────────────────────────────
-- One row per generated file: image, video or audio. Keeps the prompt, the
-- engine and the project it belongs to, so every asset is traceable.
CREATE TABLE IF NOT EXISTS media_assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,                      -- 'image' | 'video' | 'audio'
    project_key TEXT,
    prompt      TEXT,
    engine      TEXT,                                 -- 'matrix' | ...
    model       TEXT,
    path        TEXT,                                 -- local file
    source_url  TEXT,                                 -- short-lived source url
    node_id     TEXT,                                 -- connector asset id
    status      TEXT    NOT NULL DEFAULT 'ready',     -- 'ready' | 'failed'
    width       INTEGER,
    height      INTEGER,
    duration_sec REAL,
    bytes       INTEGER,
    meta        TEXT,                                 -- JSON
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_assets_project ON media_assets(project_key);
CREATE INDEX IF NOT EXISTS idx_media_assets_kind ON media_assets(kind);

-- Video generation is asynchronous (submit → task_id → poll → file), so the
-- product tracks its own job instead of holding an HTTP request open.
CREATE TABLE IF NOT EXISTS media_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT    NOT NULL,              -- 'video'
    project_key       TEXT,
    prompt            TEXT,
    engine            TEXT,
    model             TEXT,
    external_task_id  TEXT,                          -- the engine's task id
    status            TEXT    NOT NULL DEFAULT 'queued',
                                                      -- 'queued'|'running'|'succeeded'|'failed'
    progress          INTEGER NOT NULL DEFAULT 0,
    asset_id          INTEGER,                       -- FK to media_assets
    error             TEXT,
    meta              TEXT,                         -- JSON
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    FOREIGN KEY (asset_id) REFERENCES media_assets(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_media_jobs_status ON media_jobs(status);
CREATE INDEX IF NOT EXISTS idx_media_jobs_task ON media_jobs(external_task_id);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Wait up to 5s for a competing writer instead of raising "database is
    # locked" immediately — ThreadingHTTPServer serves requests concurrently.
    conn.execute("PRAGMA busy_timeout=5000")
    # Audit 2026-06-23: SQLite defaults foreign_keys to OFF per-connection.
    # Schema declares 12 FKs but they were silently ignored, allowing orphans.
    # This single line activates them across the whole app.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        # Run schema one statement at a time so we can skip ones that already exist.
        # Strip line comments first: a ';' inside a `-- ...` comment would otherwise
        # split a statement mid-way and silently drop the table (e.g. team_members).
        _schema_no_comments = "\n".join(
            re.sub(r"--.*$", "", line) for line in SCHEMA.splitlines()
        )
        for stmt in _schema_no_comments.split(";"):
            s = stmt.strip()
            if not s: continue
            try: conn.execute(s)
            except Exception: pass
        # Migrations for existing DBs
        for col in ("due_date", "assignee_id"):
            try: conn.execute(f"ALTER TABLE user_todos ADD COLUMN {col} INTEGER")
            except Exception: pass
        for col in ("workload",):
            try: conn.execute(f"ALTER TABLE team_members ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception: pass
        for col_name, col_def in [("level", "TEXT DEFAULT 'agent'"), ("manager_id", "INTEGER")]:
            try: conn.execute(f"ALTER TABLE team_members ADD COLUMN {col_name} {col_def}")
            except Exception: pass
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_team_members_manager ON team_members(manager_id)")
        except Exception: pass
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_team_members_level ON team_members(level)")
        except Exception: pass
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_user_todos_due ON user_todos(due_date)")
        except Exception: pass
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_feed_created ON activity_feed(created_at DESC)")
        except Exception: pass
        # tasks / runs are now part of the base schema (CREATE IF NOT EXISTS handles upgrades)
        # but add agent_member_id column migration for very old DBs
        for col in ("output_summary",):
            try: conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
            except Exception: pass
        for col in ("output_text",):
            try: conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
            except Exception: pass
        # Boardroom (2026-07-01): promote team_members rows that act as CEOs,
        # and let users be chairmen. Idempotent. Use explicit SQL strings with
        # single-quote literals escaped — do NOT use f-strings with `'literal'`
        # because Python tries to parse the contents as identifiers.
        for stmt in [
            "ALTER TABLE users ADD COLUMN is_chairman INTEGER NOT NULL DEFAULT 0",
        ]:
            try: conn.execute(stmt)
            except Exception: pass
        for stmt in [
            "ALTER TABLE team_members ADD COLUMN is_ceo INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE team_members ADD COLUMN project_slug TEXT",
            "ALTER TABLE team_members ADD COLUMN last_active_at INTEGER",
            "ALTER TABLE team_members ADD COLUMN status TEXT DEFAULT 'idle'",
            "ALTER TABLE team_members ADD COLUMN status_detail TEXT",
        ]:
            try: conn.execute(stmt)
            except Exception: pass
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_team_members_ceo ON team_members(is_ceo) WHERE is_ceo = 1")
        except Exception: pass
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_users_chairman ON users(is_chairman) WHERE is_chairman = 1")
        except Exception: pass
        conn.commit()

    # Audit 2026-06-23: founders table migration — add CHECK constraint
    # for badge_color. SQLite < 3.25 can't ALTER TABLE ... ADD CONSTRAINT,
    # so we recreate the table in place (preserving data + indexes).
    # Idempotent: only runs if the table exists without the constraint.
    with get_connection() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='founders'"
        ).fetchone()
        if row and "badge_color IN ('gold','silver','bronze')" not in (row[0] or ""):
            try:
                conn.execute("""
                    CREATE TABLE founders_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL UNIQUE,
                        original_plan TEXT NOT NULL,
                        original_cents INTEGER NOT NULL,
                        badge_color TEXT DEFAULT 'gold'
                            CHECK (badge_color IN ('gold','silver','bronze')),
                        note TEXT,
                        joined_at INTEGER NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                """)
                conn.execute("""
                    INSERT INTO founders_new (id, user_id, original_plan, original_cents,
                                              badge_color, note, joined_at)
                    SELECT id, user_id, original_plan, original_cents,
                           CASE WHEN badge_color IN ('gold','silver','bronze')
                                THEN badge_color ELSE 'gold' END,
                           note, joined_at
                    FROM founders
                """)
                conn.execute("DROP TABLE founders")
                conn.execute("ALTER TABLE founders_new RENAME TO founders")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_founders_user ON founders(user_id)")
                conn.commit()
            except Exception as _e:
                # If migration fails, log to stderr but don't crash the app
                import sys as _sys
                _sys.stderr.write(f"[init_db] founders CHECK migration: {_e}\n")
    # Full-text index: create, or repair the broken pre-2026-10 definition
    # (non-existent `tags` column, triggers never created) and rebuild it.
    from . import db_fts
    with get_connection() as conn:
        db_fts.ensure_documents_fts(conn)
    # Versioned migrations (migrations.py) — the modern replacement for the
    # ad-hoc `try: ALTER except: pass` block above. New schema changes go there,
    # gated by PRAGMA user_version so they run exactly once.
    from . import migrations
    with get_connection() as conn:
        migrations.run_migrations(conn)
    # Auto-load default templates on first run (idempotent)
    try:
        init_default_templates()
    except Exception:
        log.debug("init_default_templates failed", exc_info=True)
    # Migrate pre-pivot lifetime holders into founders (idempotent)
    try:
        migrate_existing_founders()
    except Exception:
        log.debug("migrate_existing_founders failed", exc_info=True)
    # Purge expired sessions (audit 2026-06-23: was never called —
        # sessions table grew without bound. Now runs at every DB init.
    try:
        purge_expired_sessions()
    except Exception:
        log.debug("purge_expired_sessions failed", exc_info=True)
    # Attribute every document to the business it belongs to, so the RAG can
    # answer about one project without citing another project's files
    # (2026-10-23). Idempotent: it only fills rows that have no owner yet.
    try:
        from . import projects as _projects_mod
        _projects_mod.backfill_document_project_keys()
    except Exception:
        log.debug("backfill_document_project_keys failed", exc_info=True)


# Optional connection reuse (upgrade #6, opt-in via OPS_DB_CONN_REUSE=1).
# Default OFF: each db() opens a fresh connection (safe, simple). When ON, a
# thread-local connection (keyed by DB_PATH so tests/demo re-open on override)
# is reused across calls to cut connect+PRAGMA churn. It is *re-entrant*: only
# the outermost db() commits, and an exception rolls back the whole nesting —
# so nested `with db()` (e.g. migrate_existing_founders -> register_founder)
# keeps one transaction instead of many. Enable only after validating.
_conn_reuse = os.environ.get("OPS_DB_CONN_REUSE") == "1"
_local = threading.local()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    if not _conn_reuse:
        conn = get_connection()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return

    key = str(DB_PATH)
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "key", None) != key:
        if conn is not None:
            try: conn.close()
            except Exception: pass
        conn = get_connection()
        _local.conn = conn
        _local.key = key
        _local.depth = 0
    _local.depth += 1
    try:
        yield conn
        if _local.depth == 1:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _local.depth -= 1


def update_document_content(doc_id: int, content: str) -> bool:
    """Update a note's content. Returns True if updated."""
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "UPDATE documents SET content = ?, mtime = ?, indexed_at = ? WHERE id = ?",
            (content, now, now, doc_id),
        )
        return cur.rowcount > 0


def delete_document(doc_id: int) -> bool:
    """Delete a document (and its FTS rows via triggers).
    Returns True if a row was removed."""
    with db() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return cur.rowcount > 0


def rename_document(doc_id: int, *, title: str | None = None, path: str | None = None) -> bool:
    """Update title and/or path. Returns True if updated."""
    sets: list[str] = []
    vals: list = []
    if title is not None:
        sets.append("title = ?")
        vals.append(title)
    if path is not None:
        sets.append("path = ?")
        vals.append(path)
    if not sets:
        return False
    sets.append("indexed_at = ?")
    vals.append(int(time.time()))
    vals.append(doc_id)
    with db() as conn:
        cur = conn.execute(
            f"UPDATE documents SET {', '.join(sets)} WHERE id = ?",
            tuple(vals),
        )
        return cur.rowcount > 0


def create_document(*, source: str, path: str, title: str | None, content: str = "") -> int:
    """Create a new document. Returns the new doc id.
    Path is required to be unique per (source, path)."""
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO documents (source, path, title, content, metadata, mtime, size, project_key, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, path, title, content, "{}", now, len(content), _project_key_for(path), now),
        )
        return cur.lastrowid or 0


def _project_key_for(path: str) -> str | None:
    """Which business owns this document path? (None when it belongs to none.)

    Imported lazily: projects.py imports db, so a module-level import here
    would be circular.
    """
    try:
        from . import projects as _projects_mod
        return _projects_mod.project_for_path(path)
    except Exception:
        return None


def get_document(doc_id: int) -> dict | None:
    """Fetch a single document by id."""
    with db() as conn:
        row = conn.execute(
            "SELECT id, source, path, title, content, mtime FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_document(
    *,
    source: str,
    path: str,
    title: str | None,
    content: str,
    metadata: dict | None,
    mtime: int | None,
    size: int | None,
    compare_content: bool = False,
) -> bool:
    """Returns True if inserted/updated, False if skipped (unchanged).

    Unchanged means same (mtime, size). Connectors whose change key can stay
    the same across a real edit (a same-length Slack edit, a Readwise tag, two
    clips in one second) pass ``compare_content=True``: equal (mtime, size) then
    also needs equal title, content and metadata to be skipped."""
    import json as _json

    metadata = metadata or {}
    metadata.setdefault("tags_text", " ".join(metadata.get("tags", []) or []))

    with db() as conn:
        cols = "mtime, size, title, content, metadata" if compare_content else "mtime, size"
        row = conn.execute(
            f"SELECT {cols} FROM documents WHERE source = ? AND path = ?",  # noqa: S608 - fixed column lists
            (source, path),
        ).fetchone()
        if row and row["mtime"] == mtime and row["size"] == size:
            if not compare_content:
                return False  # unchanged
            try:
                same_meta = _json.loads(row["metadata"] or "{}") == _json.loads(_json.dumps(metadata))
            except ValueError:
                same_meta = False
            if row["title"] == title and row["content"] == content and same_meta:
                return False

        conn.execute(
            """
            INSERT INTO documents (source, path, title, content, metadata, mtime, size, project_key, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, path) DO UPDATE SET
                title = excluded.title,
                content = excluded.content,
                metadata = excluded.metadata,
                mtime = excluded.mtime,
                size = excluded.size,
                project_key = excluded.project_key,
                indexed_at = excluded.indexed_at
            """,
            (
                source,
                path,
                title,
                content,
                _json.dumps(metadata, ensure_ascii=False),
                mtime,
                size,
                _project_key_for(path),
                int(time.time()),
            ),
        )
        return True


# ---- Date range parser ----

WEEKDAY_NAMES = {
    "lunes": 0, "martes": 1, "mi\u00e9rcoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "s\u00e1bado": 5, "sabado": 5, "domingo": 6,
}
MONTH_NAMES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

import datetime as _dt


def parse_date_range(query: str) -> tuple | None:
    """Try to extract a date range from a free-text query. Returns (since_ts, until_ts) or None."""
    q = query.lower()
    now = _dt.datetime.now()
    today = _dt.datetime(now.year, now.month, now.day)
    end_of_today = int(today.timestamp()) + 86399
    start_of_today = int(today.timestamp())
    if "hoy" in q:
        return (start_of_today, end_of_today)
    if "anteayer" in q or "antier" in q:
        y = today - _dt.timedelta(days=2)
        return (int(y.timestamp()), int(y.timestamp()) + 86399)
    if "ayer" in q:
        y = today - _dt.timedelta(days=1)
        return (int(y.timestamp()), int(y.timestamp()) + 86399)
    if "esta semana" in q:
        ws = today - _dt.timedelta(days=today.weekday())
        return (int(ws.timestamp()), end_of_today)
    if "este mes" in q:
        start = _dt.datetime(today.year, today.month, 1)
        return (int(start.timestamp()), end_of_today)
    if "este a\u00f1o" in q or "este ano" in q:
        start = _dt.datetime(today.year, 1, 1)
        return (int(start.timestamp()), end_of_today)
    if "semana pasada" in q:
        this_mon = today - _dt.timedelta(days=today.weekday())
        last_mon = this_mon - _dt.timedelta(days=7)
        last_sun = this_mon - _dt.timedelta(days=1)
        return (int(last_mon.timestamp()), int(last_sun.timestamp()) + 86399)
    if "mes pasado" in q:
        first_this = _dt.datetime(today.year, today.month, 1)
        last_month_end = first_this - _dt.timedelta(days=1)
        last_month_start = _dt.datetime(last_month_end.year, last_month_end.month, 1)
        return (int(last_month_start.timestamp()), int(last_month_end.timestamp()) + 86399)
    if "\u00faltimos 7 d\u00edas" in q or "ultimos 7 dias" in q or "\u00faltima semana" in q:
        return (int((today - _dt.timedelta(days=7)).timestamp()), end_of_today)
    if "\u00faltimos 30 d\u00edas" in q or "ultimos 30 dias" in q or "\u00faltimo mes" in q:
        return (int((today - _dt.timedelta(days=30)).timestamp()), end_of_today)
    if "\u00faltimos 90 d\u00edas" in q or "ultimos 90 dias" in q:
        return (int((today - _dt.timedelta(days=90)).timestamp()), end_of_today)
    import re
    m = re.search(r"el\s+(\d{1,2})\s+de\s+([a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1]+)(?:\s+de\s+(\d{4}))?", q)
    if m:
        day = int(m.group(1))
        month = MONTH_NAMES.get(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if month:
            try:
                d = _dt.datetime(year, month, day)
                return (int(d.timestamp()), int(d.timestamp()) + 86399)
            except ValueError:
                pass
    return None


# ---- Project prefix parser ----

PROJECT_PREFIX_RE = __import__("re").compile(r"^:([a-z0-9_\-]+)(?:\s+(.*))?$", __import__("re").IGNORECASE)


def parse_project_prefix(query: str) -> tuple[str | None, str]:
    """Parse ':project query' or ':project' alone. Returns (project_key, remaining_query)."""
    m = PROJECT_PREFIX_RE.match(query.strip())
    if m:
        return m.group(1).lower(), (m.group(2) or "").strip()
    return None, query


def _casefold(value: object) -> str | None:
    """SQL function for Unicode-aware case-insensitive matching in search()."""
    return None if value is None else str(value).casefold()


def _folded_excerpt(content: str, query: str, width: int = 220,
                    require: bool = False) -> str:
    """Excerpt around the case-folded match, at the offset in the ORIGINAL text
    (folding can change length: "ß" → "ss").

    With require=True an absent match yields an empty string instead of the
    head of the text, so a caller can tell "found here" from "fell back".
    """
    q = query.casefold()
    folded: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(content):
        f = ch.casefold()
        folded.append(f)
        origin.extend([i] * len(f))
    pos = "".join(folded).find(q)
    if pos < 0:
        return "" if require else content[:width]
    start = origin[pos] if pos >= 0 else 0
    return content[start:start + width]


def _like_search(query: str, fold: str, source: str | None, limit: int) -> list:
    with db() as conn:
        if fold == "cl_casefold":
            conn.create_function("cl_casefold", 1, _casefold, deterministic=True)
            # content is cut in Python here, see _folded_excerpt
            excerpt = "d.content AS excerpt"
            matches = ("({fold}(d.content) LIKE '%' || {fold}(?) || '%')"
                       " OR ({fold}(d.title) LIKE '%' || {fold}(?) || '%')"
                       " OR ({fold}(d.path)  LIKE '%' || {fold}(?) || '%')")
        else:
            excerpt = f"substr(d.content, MAX(1, instr({fold}(d.content), {fold}(?))), 220) AS excerpt"
            # The path is matched too: searching for a project or a repo name
            # must find its documents even when neither the title nor the body
            # mentions it (2026-10-23).
            matches = ("({fold}(d.content) LIKE '%' || {fold}(?) || '%')"
                       " OR ({fold}(d.title) LIKE '%' || {fold}(?) || '%')"
                       " OR ({fold}(d.path)  LIKE '%' || {fold}(?) || '%')")
        sql = f"""
            SELECT
                d.id, d.source, d.path, d.title,
                {excerpt},
                d.metadata
            FROM documents d
            WHERE ({matches.format(fold=fold)})
        """
        # Bindings follow the order they appear in the statement: the excerpt
        # comes first (it is in the SELECT), then the three match terms, then
        # the optional source and the limit.
        params: list = [query] * (3 if fold == "cl_casefold" else 4)
        if source:
            sql += " AND d.source = ?"
            params.append(source)
        sql += " ORDER BY length(d.content) LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    if fold != "cl_casefold":
        return list(rows)
    return [{**dict(r), "excerpt": _folded_excerpt(r["excerpt"] or "", query)} for r in rows]


# Stopwords in both languages the product is used in: dropping "de" / "the"
# keeps a two-word query from matching every document that happens to contain
# a common word.
_FTS_STOPWORDS = {
    "and", "or", "not", "a", "an", "the", "of", "to", "in", "on", "for",
    "de", "la", "el", "los", "las", "un", "una", "y", "o", "en", "con", "por",
    "para", "del", "al", "que", "es", "son", "the", "with", "from", "at", "by",
}


def _fts_query(query: str) -> str:
    """Turn a human query into a safe FTS5 MATCH expression.

    FTS5 has its own query language, so anything the user types can be a
    syntax error ("NOT", an unbalanced quote, "NEAR(...)"). Every term is
    emitted as an explicit quoted string instead: quoted input is taken
    literally, and ``*`` after it makes it a prefix match so "indicador"
    still finds "indicadores". 2026-10-23.
    """
    terms: list[str] = []
    for raw in (query or "").split():
        term = raw.strip().strip('"').strip("'").strip()
        if not term:
            continue
        if term.lower() in _FTS_STOPWORDS:
            continue
        # Skip pure operators and anything with FTS5 syntax characters.
        if not any(ch.isalnum() for ch in term):
            continue
        terms.append('"' + term.replace('"', "") + '"*')
    return " OR ".join(terms)


def _row_project(doc_id: int) -> str | None:
    """The project a document belongs to, for the LIKE-layer filter."""
    with db() as conn:
        row = conn.execute("SELECT project_key FROM documents WHERE id = ?",
                           (int(doc_id),)).fetchone()
    return row["project_key"] if row else None


def _fts_search(query: str, limit: int, source: str | None,
                project_key: str | None = None) -> list:
    """Ranked full-text search over the FTS5 index of ``documents``.

    LIKE only matches a literal substring, so it misses a query word written
    in the singular, with different spacing, or as one of several words. FTS5
    answers all of those and returns them in relevance order. 2026-10-23.
    """
    match = _fts_query(query)
    if not match:
        return []
    # documents_fts columns are (title, content, tags): content is index 1.
    # Reading index 2 silently returned the tags column, which is why a body
    # hit produced an empty excerpt.
    sql = """
        SELECT d.id, d.source, d.path, d.title,
               snippet(documents_fts, 0, '', '', '…', 12) AS title_snip,
               snippet(documents_fts, 1, '', '', '…', 14) AS content_snip,
               substr(IFNULL(d.content, ''), 1, 220) AS head,
               d.metadata,
               bm25(documents_fts, 0.0, 4.0, 1.0) AS score
        FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
    """
    params: list = [match]
    if source:
        sql += " AND d.source = ?"
        params.append(source)
    if project_key:
        sql += " AND d.project_key = ?"
        params.append(project_key)
    # bm25() is negative-better; the weights favour a title hit over a body one.
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        with db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        # A malformed MATCH must never take the endpoint down.
        log.debug("fts search failed for %r: %s", query, e)
        return []
    return [{**dict(r), "excerpt": _best_excerpt(r, query)} for r in rows]


def _best_excerpt(row, query: str) -> str:
    """Text around the match, the way the product has always shown it.

    FTS5's own snippet() returns a window of tokens, which on a short
    document is the whole thing — so a hit in the middle of the text showed
    no context at all. The case-folded offset is therefore the primary
    excerpt, because it is centred on the match; the FTS snippet and then the
    head of the document serve the cases where the words differ in form
    (stemmed, inflected) and no literal offset exists. 2026-10-23.
    """
    keys = row.keys()
    head = (row["head"] if "head" in keys else "") or ""
    if query and query.strip():
        folded = _folded_excerpt(head, query, width=220, require=True)
        if folded:
            return folded[:400]
    for key in ("content_snip", "title_snip"):
        value = ((row[key] if key in keys else "") or "").strip()
        if value:
            return value[:400]
    return head[:220]


def search(query: str, *, limit: int = 20, source: str | None = None,
           fuzzy: bool = False, project_key: str | None = None) -> list[sqlite3.Row]:
    """Search title + content, ranked.

    FTS5 first (relevance-ordered, catches word forms LIKE misses), then LIKE
    for the exact-substring cases the index does not cover, then fuzzy on
    titles for typos. Each layer only contributes rows the previous one
    missed. 2026-10-23.
    """
    if not query.strip():
        return []
    # SQLite's lower() only folds ASCII, so "DECISIÓN" never matched "decisión".
    # Non-ASCII queries fold with Python's str.casefold() (registered per
    # connection). ASCII queries try the faster built-in lower() first and fall
    # back to casefold when it finds nothing ("STRASSE" vs "Straße").
    rows: list = []
    seen_ids: set = set()
    if project_key is None:
        for row in _fts_search(query, limit, source):
            seen_ids.add(row["id"])
            rows.append(row)
    if len(rows) < limit:
        if query.isascii():
            extra = _like_search(query, "lower", source, limit)
        else:
            extra = []
        if not extra and len(rows) < limit:
            extra = _like_search(query, "cl_casefold", source, limit)
        if project_key:
            with db() as conn:
                extra = [r for r in extra if _row_project(r["id"]) == project_key]
        for r in extra:
            rid = r["id"]
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            rows.append(dict(r))
    if fuzzy and query:
        # Augment with fuzzy matches on title that neither FTS nor LIKE caught
        seen_ids = {r["id"] for r in rows}
        try:
            from rapidfuzz import fuzz, process
            with db() as conn:
                # Cap the fuzzy scan at 5000 titles to avoid loading the whole vault.
                fsql = ("SELECT id, source, path, title, content, metadata FROM documents "
                        "WHERE title IS NOT NULL AND title != ''")
                fparams: list = []
                if source:  # fuzzy matches honour the source filter too
                    fsql += " AND source = ?"
                    fparams.append(source)
                if project_key:
                    fsql += " AND project_key = ?"
                    fparams.append(project_key)
                titles = conn.execute(fsql + " ORDER BY mtime DESC LIMIT 5000", fparams).fetchall()
            scored = process.extract(query, [t["title"] for t in titles],
                                    scorer=fuzz.WRatio, limit=limit, score_cutoff=55)
            for match_str, score, idx in scored:
                if score < 55: continue
                t = titles[idx]
                if t["id"] in seen_ids: continue
                excerpt = (t["content"] or "")[:220]
                rows.append({"id": t["id"], "source": t["source"], "path": t["path"],
                             "title": t["title"], "excerpt": excerpt, "metadata": t["metadata"]})
        except Exception as e:
            log.debug("fuzzy search failed: %s", e)
    return rows


def documents_by_path(path: str, *, limit: int = 5) -> list[dict]:
    """Documents stored under exactly ``path`` (any source), search-row shaped."""
    with db() as conn:
        rows = conn.execute("SELECT id, source, path, title, substr(content, 1, 220) AS excerpt, metadata "
                            "FROM documents WHERE path = ? ORDER BY id LIMIT ?", (path, limit)).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        per_source = conn.execute(
            "SELECT source, COUNT(*) AS c FROM documents GROUP BY source ORDER BY c DESC"
        ).fetchall()
        last_runs = conn.execute(
            "SELECT source, started_at, ended_at, indexed, skipped, errors "
            "FROM index_runs ORDER BY id DESC LIMIT 10"
        ).fetchall()
        return {
            "total": total,
            "per_source": [dict(r) for r in per_source],
            "last_runs": [dict(r) for r in last_runs],
        }


def recent(*, source: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    with db() as conn:
        sql = "SELECT source, path, title, mtime, size FROM documents"
        params: list = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY mtime DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()


def recent_documents(*, limit: int = 100, source: str | None = None) -> list[dict]:
    """Like recent() but returns full rows (with id, content, etc) as dicts."""
    with db() as conn:
        sql = "SELECT id, source, path, title, mtime, size FROM documents"
        params: list = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY mtime DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def start_index_run(source: str) -> int:
    """Start an indexing run. Returns the index_runs.id."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO index_runs (source, started_at) VALUES (?, ?)",
            (source, int(time.time())),
        )
        return cur.lastrowid

# Playbook helpers moved to db_playbook.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_brain import (  # noqa: E402,F401
    add_message,
    create_conversation,
    list_conversations,
    save_conversation_turn,
)

# Graph helpers moved to db_graph.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_graph import (  # noqa: E402,F401
    activity_by_day,
    build_relations,
    todo_summary,
)
from .db_playbook import (  # noqa: E402,F401
    LIST_ITEM_RE,
    PLAYBOOK_HEADER_RE,
    PLAYBOOK_KV_RE,
    SECTION_RE,
    all_playbooks_summary,
    get_playbook,
    list_playbook_projects,
    replace_playbook_entries,
)
from .db_snapshots import (  # noqa: E402,F401
    get_previous_snapshot,
    get_snapshot,
    recent_snapshots,
    save_snapshot,
)

# Todo helpers moved to db_todos.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_todos import (  # noqa: E402,F401
    list_todos,
    replace_todos,
)

# User-todo helpers (manual list) moved to db_usertodos.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_usertodos import (  # noqa: E402,F401
    add_activity,
    add_user_todo,
    delete_user_todo,
    list_brain_conversations_text,
    list_user_todos,
    recent_activity,
    recompute_member_workload,
    update_user_todo,
    user_todo_summary,
)

# ---- Idea helpers ----

def add_idea(content: str, project_key: str | None = None, tags: list[str] | None = None) -> int:
    import json as _json
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (content, project_key, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (content, project_key, _json.dumps(tags or []), int(time.time()), int(time.time())),
        )
        new_id = int(cur.lastrowid or 0)
    # Audit 2026-06-23 H1: dead code post-`return` (unreachable). Moved
    # activity logging out of the with block, before the return, so it
    # actually fires.
    add_activity("idea_added", content[:100], project_key)
    return new_id


def list_ideas(*, status: str | None = None, project_key: str | None = None, limit: int = 200) -> list[dict]:
    import json as _json
    with db() as conn:
        sql = "SELECT * FROM ideas"
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if project_key:
            clauses.append("project_key = ?")
            params.append(project_key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["tags"] = _json.loads(d["tags"] or "[]")
        except Exception: d["tags"] = []
        out.append(d)
    return out


def update_idea(idea_id: int, **fields) -> bool:
    if not fields: return False
    allowed = {"content", "project_key", "tags", "status"}
    sets: list[str] = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "tags":
            import json as _json
            v = _json.dumps(v if isinstance(v, list) else [v])
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets: return False
    sets.append("updated_at = ?")
    params.append(int(time.time()))
    params.append(idea_id)
    with db() as conn:
        cur = conn.execute(f"UPDATE ideas SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete_idea(idea_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
        return cur.rowcount > 0


def idea_summary() -> dict:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM ideas").fetchone()["c"]
        new_count = conn.execute("SELECT COUNT(*) AS c FROM ideas WHERE status = 'new'").fetchone()["c"]
        in_progress = conn.execute("SELECT COUNT(*) AS c FROM ideas WHERE status = 'in_progress'").fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) AS c FROM ideas WHERE status = 'done'").fetchone()["c"]
        return {"total": total, "new": new_count, "in_progress": in_progress, "done": done}


def get_conversation(conversation_id: int) -> dict:
    with db() as conn:
        conv = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not conv:
            return {}
        msgs = conn.execute(
            "SELECT id, role, content, citations, provider, model, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        import json as _json
        out = []
        for m in msgs:
            d = dict(m)
            if d["citations"]:
                try:
                    d["citations"] = _json.loads(d["citations"])
                except Exception:
                    d["citations"] = []
            else:
                d["citations"] = []
            out.append(d)
        return {"conversation": dict(conv), "messages": out}


# Brain memory helpers moved to db_brainmem.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_brainmem import (  # noqa: E402,F401
    memory_get,
    memory_list,
    memory_set,
)
from .db_teams import (  # noqa: E402,F401
    add_member,
    create_team,
    delete_member,
    delete_team,
    get_member,
    get_team,
    list_members,
    list_teams,
    seed_default_teams,
    update_member,
    update_team,
)

# ---- Tag helpers ----

def add_tag(doc_id: int, tag: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO doc_tags (doc_id, tag, added_at) VALUES (?, ?, ?)",
            (doc_id, tag.strip().lower(), int(time.time())),
        )


def remove_tag(doc_id: int, tag: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM doc_tags WHERE doc_id = ? AND tag = ?", (doc_id, tag))


def tags_for_doc(doc_id: int) -> list[str]:
    with db() as conn:
        rows = conn.execute("SELECT tag FROM doc_tags WHERE doc_id = ? ORDER BY tag", (doc_id,)).fetchall()
    return [r["tag"] for r in rows]


def all_tags() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) AS c FROM doc_tags GROUP BY tag ORDER BY c DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def docs_by_tag(tag: str, limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.source, d.path, d.title, d.mtime
            FROM documents d
            JOIN doc_tags t ON t.doc_id = d.id
            WHERE t.tag = ?
            ORDER BY d.mtime DESC
            LIMIT ?
            """,
            (tag.lower(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def export_conversation_markdown(conversation_id: int) -> str:
    """Render a conversation as markdown for export."""
    data = get_conversation(conversation_id)
    if not data:
        return ""
    conv = data["conversation"]
    from datetime import datetime
    lines = [f"# {conv['title'] or 'Conversation'}"]
    lines.append("")
    lines.append(f"_{datetime.fromtimestamp(conv['created_at']).strftime('%Y-%m-%d %H:%M')}_")
    lines.append("")
    for m in data["messages"]:
        who = m["role"].capitalize()
        if m["role"] == "assistant":
            who = f"**Brain** ({m.get('provider') or ''}{m.get('model') and ' · ' + m['model'] or ''})"
        else:
            who = f"**{who}**"
        ts = datetime.fromtimestamp(m["created_at"]).strftime("%H:%M")
        lines.append(f"## {who} — {ts}")
        lines.append("")
        lines.append(m["content"])
        if m.get("citations"):
            lines.append("")
            lines.append("### Citations")
            for c in m["citations"]:
                src = c.get("source", "")
                title = c.get("title") or c.get("path", "").split("/")[-1]
                path = c.get("path", "")
                lines.append(f"- [{src}] {title} — `{path}`")
        lines.append("")
    return "\n".join(lines)


def end_run(run_id: int, *, indexed: int, skipped: int, errors: int, notes: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE index_runs SET ended_at = ?, indexed = ?, skipped = ?, errors = ?, notes = ? "
            "WHERE id = ?",
            (int(time.time()), indexed, skipped, errors, notes, run_id),
        )


# Moved to db_tasks.py (2026-07-20). Re-exported so `db.<name>` keeps working.
# Moved to db_codetodos.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_codetodos import (  # noqa: E402,F401
    code_todo_summary,
    delete_voice_note,
    list_code_todos,
    mark_code_todo_done,
    mark_url_changes_notified,
    replace_code_todos,
    update_decision,
)

# Moved to db_costs.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_costs import (  # noqa: E402,F401
    add_cost,
    cost_summary,
    delete_cost,
    list_costs,
)

# Moved to db_decisions.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_decisions import (  # noqa: E402,F401
    add_decision,
    delete_decision,
    get_decision,
    list_decisions,
)

# Moved to db_github.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_github import (  # noqa: E402,F401
    github_summary,
    list_github_items,
    upsert_github_item,
)

# Moved to db_hierarchy.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_hierarchy import (  # noqa: E402,F401
    LEVEL_ORDER,
    get_member_chain,
    get_member_reports,
    get_org_tree,
    hierarchy_balance_suggestions,
    set_member_level,
    set_member_manager,
)
from .db_tasks import (  # noqa: E402,F401
    create_task,
    finish_run,
    get_run,
    get_task,
    list_runs,
    list_tasks,
    start_run,
    task_stats,
    update_task,
)

# Moved to db_tasktemplates.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_tasktemplates import (  # noqa: E402,F401
    delete_template,
    get_template,
    instantiate_template,
    list_templates,
    upsert_template,
)

# Moved to db_urlchanges.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_urlchanges import (  # noqa: E402,F401
    latest_url_state,
    list_url_changes,
    mark_url_change_notified,
    record_url_change,
)

# Moved to db_voice.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_voice import (  # noqa: E402,F401
    add_voice_note,
    list_voice_notes,
    search_voice_notes,
)

# ============================================================
# Back-compat aliases (CLI was written against these names)
# ============================================================

def init_default_templates() -> None:
    """Alias for task_templates.seed_defaults()."""
    from . import task_templates as _tt
    _tt.seed_defaults()


def create_decision(
    title: str,
    decision: str,
    *,
    project_key: str | None = None,
    context: str | None = None,
    consequences: str | None = None,
    links: list[dict] | None = None,
    tags: list[str] | None = None,
    decided_at: int | None = None,
) -> int:
    """Alias for add_decision."""
    return add_decision(
        title=title,
        decision=decision,
        project_key=project_key,
        context=context,
        consequences=consequences,
        links=links,
        tags=tags,
        decided_at=decided_at,
    )


# ============================================================
# Aliases for server.py compatibility
# ============================================================

def decisions_summary(*, project: str | None = None) -> dict:
    rows = list_decisions(project=project, limit=1000)
    return {"total": len(rows), "by_project": {}}


def costs_summary(*, period: str | None = None) -> dict:
    return cost_summary(period=period)


def recent_url_changes(*, hours: int = 168, limit: int = 50) -> list[dict]:
    import time as _t
    since = int(_t.time()) - (hours * 3600)
    rows = list_url_changes(limit=limit)
    return [r for r in rows if r.get("changed_at", 0) >= since]


# ============================================================
# More aliases (server.py uses project_key=, my funcs use project=)
# ============================================================

def _list_code_todos_compat(*, project_key=None, project=None, **kw):
    return list_code_todos(project=project or project_key, **kw)

def _list_decisions_compat(*, project_key=None, project=None, **kw):
    return list_decisions(project=project or project_key, **kw)

def _list_costs_compat(*, project_key=None, project=None, **kw):
    return list_costs(project=project or project_key, **kw)

# Monkey-patch: replace functions with kwarg-flexible versions
import functools as _ft


def _wrap(orig):
    @_ft.wraps(orig)
    def inner(*args, **kwargs):
        if 'project_key' in kwargs and 'project' not in kwargs:
            kwargs['project'] = kwargs.pop('project_key')
        return orig(*args, **kwargs)
    return inner

list_code_todos = _wrap(list_code_todos)
list_decisions = _wrap(list_decisions)
list_costs = _wrap(list_costs)
list_voice_notes = _wrap(list_voice_notes)
search_voice_notes = _wrap(search_voice_notes)


# Moved to db_agents.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_agents import (  # noqa: E402,F401
    add_agent_memory,
    add_trigger,
    delete_agent_memory,
    delete_trigger,
    get_agent_memory,
    get_trigger_by_token,
    list_agent_logs,
    list_agent_memory,
    list_all_enabled_triggers,
    list_triggers,
    log_agent_action,
    recent_agent_activity,
    record_trigger_fire,
    top_agent_memory,
    update_agent_memory,
    update_trigger,
)

# Moved to db_approvals.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_approvals import (  # noqa: E402,F401
    add_approval,
    list_pending_approvals,
    list_pending_approvals_count,
    resolve_approval,
)

# Media assets and jobs (2026-10-23). Re-exported so `db.<name>` keeps working.
from .db_media import (  # noqa: E402,F401
    add_asset,
    add_job,
    delete_asset,
    get_asset,
    get_job,
    get_job_by_external_task,
    list_assets,
    list_jobs,
    update_job,
)

# Users / auth / sessions / device tokens / usage / entitlements moved to
# db_auth.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_auth import (  # noqa: E402,F401
    create_device_token,
    create_session,
    create_user,
    delete_session,
    get_device_token,
    get_entitlement,
    get_session,
    get_user,
    get_user_by_email,
    grant_entitlement,
    list_device_tokens,
    list_entitlements,
    log_usage,
    purge_expired_sessions,
    revoke_device_token,
    revoke_entitlement,
    touch_device_token,
    update_user,
    usage_summary,
    verify_password,
)

# Founders pricing, founder registry, and payment-event log moved to
# db_billing.py (2026-07-20). Re-exported here so `db.<name>` keeps working.
from .db_billing import (  # noqa: E402,F401
    FINAL_PRICE_CENTS,
    PRICING_TIERS,
    current_tier,
    get_founder,
    is_founder,
    is_payment_event_seen,
    list_founders,
    log_payment_event,
    migrate_existing_founders,
    register_founder,
)

# External connections moved to db_connections.py (2026-07-20). Re-exported
# so `db.<name>` keeps working.
from .db_connections import (  # noqa: E402,F401
    add_connection,
    delete_connection,
    get_connection_by_id,
    get_connection_by_name,
    list_connections,
    record_connection_check,
    record_connection_used,
    update_connection,
)

# Moved to db_ga.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_ga import (  # noqa: E402,F401
    add_ga_site,
    delete_ga_site,
    ga_daily_totals,
    ga_summary,
    get_ga_site,
    list_ga_metrics_daily,
    list_ga_sites,
    record_ga_site_poll,
    revenue_margin_by_project,
    update_ga_site,
    upsert_ga_metrics_daily,
)

# Google Search Console (2026-07-23). Re-exported so `db.<name>` keeps working.
from .db_gsc import (  # noqa: E402,F401
    add_gsc_site,
    delete_gsc_site,
    get_gsc_site,
    gsc_daily,
    gsc_summary,
    list_gsc_sites,
    record_gsc_site_poll,
    update_gsc_site,
    upsert_gsc_metrics_daily,
)

# Moved to db_mcp.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_mcp import (  # noqa: E402,F401
    bind_agent_tool,
    cache_mcp_tools,
    delete_mcp_server,
    get_mcp_server,
    list_agent_tools,
    list_mcp_servers,
    set_mcp_server_error,
    unbind_agent_tool,
    upsert_mcp_server,
)

# Note backlinks moved to db_notelinks.py (2026-07-20). Re-exported so
# `db.<name>` keeps working.
from .db_notelinks import (  # noqa: E402,F401
    add_note_link,
    count_links,
    get_backlinks,
    get_doc_id_by_path,
    get_doc_id_by_title,
    get_outgoing_links,
)

# Moved to db_revenue.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_revenue import (  # noqa: E402,F401
    GA_FORECAST_METRICS,
    add_revenue_source,
    customer_cohorts,
    delete_revenue_source,
    ga_forecast,
    get_revenue_source,
    get_revenue_source_by_name,
    insert_revenue_event,
    insert_revenue_events_bulk,
    list_revenue_events,
    list_revenue_sources,
    record_revenue_source_poll,
    revenue_daily_buckets,
    revenue_forecast,
    revenue_summary,
    update_revenue_source,
)

# Moved to db_sentry.py (2026-07-20). Re-exported so `db.<name>` keeps working.
from .db_sentry import (  # noqa: E402,F401
    add_sentry_project,
    delete_sentry_project,
    get_sentry_project,
    get_sentry_project_by_name,
    list_sentry_issues,
    list_sentry_projects,
    record_sentry_project_poll,
    sentry_summary,
    update_sentry_project,
    upsert_sentry_issue,
)
