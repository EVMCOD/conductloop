"""Versioned DB migrations.

Uses SQLite's built-in ``PRAGMA user_version`` as the fast applied-version gate,
plus a ``schema_migrations`` audit table for a human-readable history. Each
migration is ``(version, description, fn(conn))`` and runs exactly once, in
order, when the DB's user_version is below it.

New schema changes go HERE — not as `try: ALTER TABLE ... except: pass` blocks
in init_db (those swallow real errors and re-run every startup). Add a function,
append it to MIGRATIONS with the next integer version, done.

    def _mig_3_add_widget(conn):
        conn.execute("ALTER TABLE things ADD COLUMN widget TEXT")
    MIGRATIONS.append((3, "add things.widget", _mig_3_add_widget))
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

log = logging.getLogger("conductloop.migrations")

# Everything created by SCHEMA + the legacy ALTER block in init_db() is treated
# as baseline. Real migrations start at version 2.
BASELINE = 1


def _mig_2_schema_migrations_audit(conn: sqlite3.Connection) -> None:
    """Create the audit table that records every applied migration."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  description TEXT NOT NULL,"
        "  applied_at INTEGER NOT NULL)"
    )


def _mig_3_captures(conn: sqlite3.Connection) -> None:
    """Quick-capture inbox: anything dumped in from anywhere, processed later."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS captures ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  content TEXT NOT NULL,"
        "  kind TEXT NOT NULL DEFAULT 'note',"      # note | url | idea | task
        "  source TEXT,"
        "  url TEXT,"
        "  processed INTEGER NOT NULL DEFAULT 0,"
        "  created_at INTEGER NOT NULL,"
        "  processed_at INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_captures_open ON captures(processed, created_at)")


def _mig_4_calendar(conn: sqlite3.Connection) -> None:
    """Calendar events imported from ICS feeds, for the briefing's agenda."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calendar_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  uid TEXT UNIQUE,"
        "  title TEXT NOT NULL,"
        "  starts_at INTEGER NOT NULL,"
        "  all_day INTEGER NOT NULL DEFAULT 0,"
        "  source TEXT,"
        "  created_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calendar_starts ON calendar_events(starts_at)")


def _mig_5_memory_embeddings(conn: sqlite3.Connection) -> None:
    """Cached embeddings for brain_memory keys, so semantic recall embeds only
    the query at read time instead of re-embedding every memory each call."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_embeddings ("
        "  key TEXT PRIMARY KEY,"
        "  vector TEXT NOT NULL,"
        "  model TEXT NOT NULL,"
        "  indexed_at INTEGER NOT NULL)"
    )


# (version:int, description:str, fn(conn)) — keep sorted, versions contiguous.
def _mig_6_ga_sites_source_type(conn: sqlite3.Connection) -> None:
    """ga_sites.source_type: the GA indexer selects its source class from this
    column, but the schema never had it — every site silently polled as mock.
    Backfill: rows carrying a service account JSON are real GA4 properties.

    Skips cleanly when the table doesn't exist yet (fresh DB: the baseline
    SCHEMA now creates the column) or when the column is already present."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ga_sites'"
    ).fetchone()
    if not row:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ga_sites)")]
    if "source_type" in cols:
        return
    conn.execute("ALTER TABLE ga_sites ADD COLUMN source_type TEXT")
    conn.execute(
        "UPDATE ga_sites SET source_type = CASE "
        "WHEN service_account_json IS NOT NULL AND service_account_json != '' "
        "THEN 'ga4' ELSE 'mock' END"
    )


def _mig_7_gsc_tables(conn: sqlite3.Connection) -> None:
    """Google Search Console: site registry + daily search metrics.
    Mirrors the ga_sites / ga_metrics_daily pattern."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gsc_sites ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  site_url TEXT UNIQUE NOT NULL,"            # 'sc-domain:example.com' | 'https://example.com/'
        "  display_name TEXT NOT NULL,"
        "  project_key TEXT NOT NULL,"
        "  credentials_json TEXT,"                    # OAuth authorized_user blob (or SA key)
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  last_poll_at INTEGER,"
        "  last_poll_status TEXT,"
        "  last_poll_error TEXT,"
        "  created_at INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gsc_sites_project ON gsc_sites(project_key)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gsc_metrics_daily ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  site_id INTEGER NOT NULL,"
        "  site_url TEXT NOT NULL,"
        "  project_key TEXT NOT NULL,"
        "  date TEXT NOT NULL,"                       # YYYY-MM-DD
        "  clicks INTEGER NOT NULL DEFAULT 0,"
        "  impressions INTEGER NOT NULL DEFAULT 0,"
        "  ctr REAL,"                                 # 0..1
        "  position REAL,"                            # average position
        "  raw_payload TEXT,"
        "  created_at INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL,"
        "  UNIQUE(site_id, date))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gsc_daily_site ON gsc_metrics_daily(site_id, date)")


# Multi-agent sessions (design doc §4, decisions D3/D4/D5/D7/D8). Additive only:
# seven new tables, no change to any existing table. Values are enforced twice —
# here with CHECKs (last line of defence) and in ops_dashboard/sessions/.
_SESSION_STATUSES = ("'created','provisioning','idle','running','waiting_human',"
                     "'paused','stalled','closing','closed','failed'")
_TASK_STATUSES = ("'queued','claimed','running','needs_review','done','blocked',"
                  "'failed','cancelled'")


def _mig_8_agent_sessions(conn: sqlite3.Connection) -> None:
    # D7: sessions may only run on repos the user explicitly marked.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_repos ("
        "  repo_path TEXT PRIMARY KEY,"
        "  project_key TEXT,"
        "  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),"
        "  created_at INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_sessions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL CHECK (length(trim(name)) > 0),"
        "  role TEXT NOT NULL CHECK (length(trim(role)) > 0),"
        "  scope_globs TEXT NOT NULL DEFAULT '[]',"         # JSON list of globs
        "  provider TEXT NOT NULL CHECK (length(provider) > 0),"
        "  model TEXT NOT NULL CHECK (length(model) > 0),"
        # D5: a secrets_store item NAME (e.g. 'byok.anthropic.default'), never a key value.
        "  key_ref TEXT CHECK (key_ref IS NULL OR (length(key_ref) BETWEEN 6 AND 96"
        "    AND key_ref GLOB 'byok.*' AND key_ref NOT GLOB '*[^A-Za-z0-9._-]*')),"
        "  project_key TEXT,"
        "  repo_path TEXT NOT NULL REFERENCES session_repos(repo_path),"
        "  base_ref TEXT NOT NULL CHECK (length(base_ref) > 0),"
        "  worktree_path TEXT,"
        "  branch TEXT,"
        f"  status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ({_SESSION_STATUSES})),"
        # D4: mandatory budget in EUR. Iterations default to 60; the upper limit is
        # enforced in code (sessions.limits) so raising it needs no migration.
        "  budget_currency TEXT NOT NULL DEFAULT 'EUR' CHECK (budget_currency = 'EUR'),"
        "  budget_cost_cents INTEGER NOT NULL CHECK (budget_cost_cents > 0),"
        "  budget_tokens INTEGER CHECK (budget_tokens IS NULL OR budget_tokens > 0),"
        "  max_iterations INTEGER NOT NULL DEFAULT 60 CHECK (max_iterations >= 1),"
        "  iterations INTEGER NOT NULL DEFAULT 0,"
        "  tokens_in INTEGER NOT NULL DEFAULT 0,"
        "  tokens_out INTEGER NOT NULL DEFAULT 0,"
        "  cost_cents_est REAL NOT NULL DEFAULT 0,"
        "  agent_member_id INTEGER,"                         # weak ref to team_members
        "  heartbeat_at INTEGER,"
        "  last_error TEXT,"
        "  created_at INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL,"
        "  closed_at INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asessions_status ON agent_sessions(status)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_tasks ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  title TEXT NOT NULL CHECK (length(trim(title)) > 0),"
        "  prompt TEXT NOT NULL,"
        "  role_hint TEXT,"
        "  paths_hint TEXT NOT NULL DEFAULT '[]',"          # JSON globs the task expects to touch
        # D8 (v1): the USER assigns a task to a session; only that session may claim it.
        "  assigned_session_id INTEGER REFERENCES agent_sessions(id),"
        "  owner_session_id INTEGER REFERENCES agent_sessions(id),"
        f"  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ({_TASK_STATUSES})),"
        "  priority INTEGER NOT NULL DEFAULT 0,"
        "  depends_on TEXT NOT NULL DEFAULT '[]',"           # JSON list of session_tasks ids
        "  legacy_task_id INTEGER,"                          # weak refs to tasks / runs
        "  run_id INTEGER,"
        "  result_summary TEXT,"
        "  commits TEXT NOT NULL DEFAULT '[]',"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  created_by TEXT,"
        "  claimed_at INTEGER,"
        "  created_at INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL,"
        # One owner per task: in-flight states need an owner, a queued task has none.
        "  CHECK (status NOT IN ('claimed','running','needs_review') OR owner_session_id IS NOT NULL),"
        "  CHECK (status <> 'queued' OR owner_session_id IS NULL))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stasks_queue "
                 "ON session_tasks(status, assigned_session_id, priority DESC, id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS path_claims ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  session_id INTEGER NOT NULL REFERENCES agent_sessions(id),"
        "  task_id INTEGER REFERENCES session_tasks(id),"
        "  repo_path TEXT NOT NULL,"
        "  pattern TEXT NOT NULL CHECK (length(pattern) > 0),"   # glob relative to the repo
        # D3: claims are required to write; hot files (server.py, index.html,
        # db.py, lockfiles) are always 'exclusive'.
        "  mode TEXT NOT NULL DEFAULT 'write' CHECK (mode IN ('write', 'exclusive')),"
        "  created_at INTEGER NOT NULL,"
        "  expires_at INTEGER,"
        "  released_at INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_active ON path_claims(repo_path, released_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_session ON path_claims(session_id, released_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_messages ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  from_session INTEGER REFERENCES agent_sessions(id),"   # NULL = human / system
        "  to_session INTEGER REFERENCES agent_sessions(id),"     # NULL = human / broadcast
        "  task_id INTEGER REFERENCES session_tasks(id),"
        "  kind TEXT NOT NULL CHECK (kind IN ('info','request','handoff','conflict','system')),"
        "  body TEXT NOT NULL,"
        "  created_at INTEGER NOT NULL,"
        "  read_at INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_smessages_to ON session_messages(to_session, read_at, id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  session_id INTEGER NOT NULL REFERENCES agent_sessions(id),"
        "  task_id INTEGER REFERENCES session_tasks(id),"
        "  kind TEXT NOT NULL CHECK (length(kind) > 0),"
        "  payload TEXT,"                                    # JSON
        "  tokens_in INTEGER NOT NULL DEFAULT 0,"
        "  tokens_out INTEGER NOT NULL DEFAULT 0,"
        "  cost_cents_est REAL NOT NULL DEFAULT 0,"
        "  created_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sevents_session ON session_events(session_id, id)")
    # User-entered prices only (EUR cents per million tokens). No seed rows.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS model_prices ("
        "  provider TEXT NOT NULL,"
        "  model TEXT NOT NULL,"
        "  currency TEXT NOT NULL DEFAULT 'EUR' CHECK (currency = 'EUR'),"
        "  in_cents_per_mtok REAL NOT NULL CHECK (in_cents_per_mtok >= 0),"
        "  out_cents_per_mtok REAL NOT NULL CHECK (out_cents_per_mtok >= 0),"
        "  updated_at INTEGER NOT NULL,"
        "  PRIMARY KEY (provider, model))"
    )


MIGRATIONS: list[tuple[int, str, Callable]] = [
    (2, "schema_migrations audit table", _mig_2_schema_migrations_audit),
    (3, "quick-capture inbox table", _mig_3_captures),
    (4, "calendar_events table", _mig_4_calendar),
    (5, "memory_embeddings cache", _mig_5_memory_embeddings),
    (6, "ga_sites.source_type column", _mig_6_ga_sites_source_type),
    (7, "gsc_sites + gsc_metrics_daily tables", _mig_7_gsc_tables),
    (8, "multi-agent sessions: repos, sessions, tasks, path claims, messages, events, prices",
     _mig_8_agent_sessions),
]

# ── Connectors framework (research 2026-10-21-conductloop-connectors.md).
# Placed here (not at EOF) so it neither touches the sessions (8) list hunk nor
# the learning (9+) EOF appends. Additive and idempotent (GAP_FILL_FROM): state
# only; OAuth tokens never land in SQLite (OS keychain via secrets_store).
def _mig_14_connectors(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS connector_accounts ("
        "  connector_id TEXT PRIMARY KEY,"
        "  status TEXT NOT NULL DEFAULT 'disconnected'"
        "    CHECK (status IN ('connected','disconnected','error')),"
        "  permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write')),"
        "  granted_scopes TEXT NOT NULL DEFAULT '[]',"
        "  account_label TEXT,"
        "  client_id TEXT,"
        "  last_error TEXT,"
        "  connected_at INTEGER,"
        "  updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS connector_actions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  connector_id TEXT NOT NULL,"
        "  action TEXT NOT NULL,"
        "  payload TEXT NOT NULL,"
        "  payload_hash TEXT NOT NULL,"
        "  summary TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'pending'"
        "    CHECK (status IN ('pending','rejected','expired','done','failed')),"
        "  result TEXT,"
        "  created_at INTEGER NOT NULL,"
        "  expires_at INTEGER NOT NULL,"
        "  decided_at INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_connector_actions_status "
                 "ON connector_actions(status, created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS connector_audit ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts INTEGER NOT NULL,"
        "  connector_id TEXT NOT NULL,"
        "  event TEXT NOT NULL,"
        "  detail TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_connector_audit_ts ON connector_audit(ts)")


MIGRATIONS.append((14, "connectors framework: accounts state, pending writes, audit",
                   _mig_14_connectors))


# MCP client (stacked on the connectors framework): per-tool policy. Tool
# descriptions come from the server and are untrusted; ``desc_hash`` lets the
# hub notice when a server changes a tool after the user reviewed it.
def _mig_15_mcp_tool_policies(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mcp_tool_policies ("
        "  server TEXT NOT NULL,"
        "  tool TEXT NOT NULL,"
        "  mode TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto','read','write','disabled')),"
        "  desc_hash TEXT NOT NULL DEFAULT '',"
        "  changed INTEGER NOT NULL DEFAULT 0,"
        "  updated_at INTEGER NOT NULL,"
        "  PRIMARY KEY (server, tool))"
    )


MIGRATIONS.append((15, "mcp tool policies (read-only by default, rug-pull detection)", _mig_15_mcp_tool_policies))


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


# Migrations from this version on are developed on parallel branches (e.g.
# sessions = 8, learning = 9) and may land in any order. A DB already at 9
# would otherwise skip 8 forever, so versions >= GAP_FILL_FROM that are below
# user_version but missing from the schema_migrations audit table are applied
# late. Every migration >= GAP_FILL_FROM MUST be idempotent (CREATE ... IF NOT
# EXISTS, no bare ALTER/INSERT). Older migrations are never re-run.
GAP_FILL_FROM = 8


def _audited_versions(conn: sqlite3.Connection) -> set[int] | None:
    """Versions recorded in schema_migrations, or None if the table is absent."""
    try:
        return {int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations")}
    except sqlite3.Error:
        return None


def _needs_gap_fill(version: int, audited: set[int] | None) -> bool:
    return audited is not None and version >= GAP_FILL_FROM and version not in audited


def run_migrations(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Apply every migration newer than the DB's user_version, in order.
    Returns the list of (version, description) applied this run."""
    cur = current_version(conn)
    if cur < BASELINE:
        conn.execute(f"PRAGMA user_version = {BASELINE}")
        cur = BASELINE

    audited = _audited_versions(conn)
    applied: list[tuple[int, str]] = []
    for version, description, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version <= cur and not _needs_gap_fill(version, audited):
            continue
        try:
            fn(conn)
            # Never lower user_version when filling a gap (see GAP_FILL_FROM).
            conn.execute(f"PRAGMA user_version = {max(version, current_version(conn))}")
            # Best-effort audit row (table exists from migration 2 onward).
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) "
                    "VALUES (?, ?, ?)",
                    (version, description, int(time.time())),
                )
            except sqlite3.Error:
                pass
            applied.append((version, description))
            log.info("applied migration %d: %s", version, description)
        except Exception:
            log.exception("migration %d (%s) failed", version, description)
            raise
    return applied


# ── Learning (design doc 2026-10-21-conductloop-learning-design.md §3.3/§4,
# PR L3; decisions D6/D7/D15). Additive and idempotent (GAP_FILL_FROM): three
# new tables, no change to any existing table. Appended at the end of the file
# so it does not touch the sessions migration (8) hunk.
_FEEDBACK_TARGETS = "'brain_message','boardroom_message','agent_run','session_task'"
_FEEDBACK_REASONS = "'wrong','missing_citation','bad_citation','outdated','style','unsafe','other'"
_FAILURE_KINDS = "'provider_error','timeout','broken_citation','tool_error','budget','eval_regression'"


def _mig_9_learning_feedback(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_settings ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL,"
        "  updated_at INTEGER NOT NULL)"
    )
    # D7: thumbs are always stored locally (explicit user action); one vote per target.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feedback ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        f"  target_kind TEXT NOT NULL CHECK (target_kind IN ({_FEEDBACK_TARGETS})),"
        "  target_id INTEGER NOT NULL,"
        "  rating INTEGER NOT NULL CHECK (rating IN (-1, 1)),"
        f"  reason_code TEXT CHECK (reason_code IS NULL OR reason_code IN ({_FEEDBACK_REASONS})),"
        "  comment TEXT CHECK (comment IS NULL OR length(comment) <= 2000),"
        "  provider TEXT,"
        "  model TEXT,"
        "  created_at INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL,"
        "  UNIQUE (target_kind, target_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at)")
    # D6: opt-in failure log, error CODES only (no prompts, answers or context),
    # 30-day retention enforced in ops_dashboard.learning.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS failure_log ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        f"  kind TEXT NOT NULL CHECK (kind IN ({_FAILURE_KINDS})),"
        "  code TEXT NOT NULL CHECK (length(code) BETWEEN 1 AND 64),"
        "  provider TEXT CHECK (provider IS NULL OR length(provider) <= 64),"
        "  model TEXT CHECK (model IS NULL OR length(model) <= 128),"
        "  route TEXT CHECK (route IS NULL OR length(route) <= 128),"
        "  created_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_log_created ON failure_log(created_at)")


MIGRATIONS.append((9, "learning: settings, feedback (thumbs), opt-in failure log", _mig_9_learning_feedback))


# ── Skills (design doc §3.6/§4, PR L5; D2/D13/D15). Idempotent (GAP_FILL_FROM):
# ADD COLUMN only if missing, CREATE ... IF NOT EXISTS. ``skills`` is rebuilt
# by the indexer on every run, so per-skill user state (enabled, activation
# counters) lives in its own table instead of extra ``skills`` columns.
_SKILLS_BASE = (
    "CREATE TABLE IF NOT EXISTS skills ("
    "  key TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,"
    "  subcategory TEXT, origin TEXT, path TEXT NOT NULL, mtime INTEGER, content_len INTEGER DEFAULT 0,"
    "  indexed_at INTEGER NOT NULL)"
)
_SKILLS_NEW_COLUMNS = (
    ("source_kind", "TEXT NOT NULL DEFAULT 'vault'"),   # vault | agents_dir | claude_dir (| project)
    ("diagnostics", "TEXT NOT NULL DEFAULT '[]'"),      # tolerant-validation warnings/errors (JSON)
    ("loadable", "INTEGER NOT NULL DEFAULT 1"),         # 0 = has errors, never in the catalog
)


def _mig_10_skills_read(conn: sqlite3.Connection) -> None:
    conn.execute(_SKILLS_BASE)
    have = {r[1] for r in conn.execute("PRAGMA table_info(skills)")}
    for col, decl in _SKILLS_NEW_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE skills ADD COLUMN {col} {decl}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skill_state ("
        "  key TEXT PRIMARY KEY,"
        "  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),"
        "  activations INTEGER NOT NULL DEFAULT 0,"
        "  last_activated_at INTEGER,"
        "  updated_at INTEGER NOT NULL)"
    )
    # D13: project skills only for repos the user marked as trusted (off by default).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skill_repo_trust ("
        "  repo_path TEXT PRIMARY KEY CHECK (length(repo_path) BETWEEN 1 AND 4096),"
        "  trusted INTEGER NOT NULL DEFAULT 0 CHECK (trusted IN (0, 1)),"
        "  updated_at INTEGER NOT NULL)"
    )


MIGRATIONS.append((10, "skills: source/diagnostics columns, skill_state, repo trust", _mig_10_skills_read))


# ── Lessons (design doc §3.4/§4, PR L4; D3/D11/D12/D15). Idempotent
# (GAP_FILL_FROM). Every lesson starts 'proposed'; only the user activates it.
def _mig_11_lessons(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lessons ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  text TEXT NOT NULL CHECK (length(trim(text)) BETWEEN 1 AND 600),"
        "  scope TEXT NOT NULL DEFAULT 'global' CHECK (scope = 'global' OR scope LIKE 'project:_%' OR scope LIKE 'repo:/%'),"
        "  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','active','rejected','archived')),"
        "  source_kind TEXT NOT NULL CHECK (source_kind IN ('feedback','session_review','correction','manual')),"
        "  source_ref TEXT,"
        "  supersedes_id INTEGER REFERENCES lessons(id),"
        "  provider TEXT, model TEXT,"
        "  cost_cents_est REAL NOT NULL DEFAULT 0,"
        "  use_count INTEGER NOT NULL DEFAULT 0,"
        "  last_used_at INTEGER,"
        "  created_at INTEGER NOT NULL,"
        "  reviewed_at INTEGER,"
        "  review_note TEXT,"
        "  updated_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_active ON lessons(status, scope)")
    # Which lessons were injected into which answer (sha256 of the answer text,
    # never the text): lets a 👎 point back at the lessons it used.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lesson_uses ("
        "  answer_sha TEXT NOT NULL CHECK (length(answer_sha) = 64),"
        "  lesson_id INTEGER NOT NULL,"
        "  created_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lesson_uses_sha ON lesson_uses(answer_sha)")


MIGRATIONS.append((11, "lessons: reviewable lessons + lesson_uses", _mig_11_lessons))


# ── Agent-created skills: approval queue (design doc §3.5/§4, PR L6; D3/D4/D5/
# D14/D15). Idempotent (GAP_FILL_FROM). Proposed content lives here, never on
# disk, until the user approves it.
def _mig_12_skill_proposals(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skill_proposals ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  action TEXT NOT NULL CHECK (action IN ('create','patch','delete','archive')),"
        "  skill_name TEXT NOT NULL CHECK (length(skill_name) BETWEEN 1 AND 64),"
        "  content TEXT,"
        "  files TEXT NOT NULL DEFAULT '{}',"
        "  base_hash TEXT,"
        "  content_hash TEXT NOT NULL,"
        "  lint TEXT NOT NULL DEFAULT '[]',"
        "  source_kind TEXT NOT NULL CHECK (source_kind IN"
        "    ('conversation','session_task','note','folder','url','feedback','curator','manual')),"
        "  source_ref TEXT,"
        "  provider TEXT, model TEXT,"
        "  cost_cents_est REAL NOT NULL DEFAULT 0,"
        "  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN"
        "    ('pending','approved','rejected','superseded','failed')),"
        "  created_at INTEGER NOT NULL,"
        "  reviewed_at INTEGER,"
        "  review_note TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON skill_proposals(status, skill_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_proposals_hash ON skill_proposals(content_hash)")


MIGRATIONS.append((12, "skill_proposals: agent-created skills approval queue", _mig_12_skill_proposals))


# ── Learning metrics: optional local A/B of lessons (design doc §7, PR L8).
# Idempotent (GAP_FILL_FROM). Only written when the user turns the A/B on
# (off by default); stores the answer's sha256, never its text.
def _mig_13_learning_ab(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_ab ("
        "  answer_sha TEXT NOT NULL CHECK (length(answer_sha) = 64),"
        "  arm TEXT NOT NULL CHECK (arm IN ('lessons','holdout')),"
        "  created_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_learning_ab_sha ON learning_ab(answer_sha)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_learning_ab_created ON learning_ab(created_at)")


MIGRATIONS.append((13, "learning_ab: optional local A/B of lessons", _mig_13_learning_ab))
# ── 20: browser sessions (browser design PR 8, decision B11) ─────────────────
# Appended at the end of the module on purpose: 9-13 (learning), 14 (#40),
# 15 (#46) and 16-19 (OAuth) are taken by parallel branches.
#
# agent_sessions gets ``browser_mode`` ('off' | 'read' | 'act') and repo_path /
# base_ref become optional: a browser session has no repo and a code session
# has no browser — never both (least privilege, B11):
#   CHECK ((browser_mode = 'off') = (repo_path IS NOT NULL))
# SQLite cannot relax NOT NULL in place, so the table is rebuilt from its own
# stored SQL (keeps any column other branches add later) with FKs off and a
# foreign_key_check before committing. Idempotent (GAP_FILL_FROM rules of #40): it does
# nothing when browser_mode already exists.

_BROWSER_MODES = ("off", "read", "act")


def _mig_20_browser_sessions(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' "
                       "AND name = 'agent_sessions'").fetchone()
    if row is None:
        return  # sessions not installed (migration 8 absent): nothing to do
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")]
    if "browser_mode" in cols:
        return
    sql = str(row[0])
    swaps = (
        ("repo_path TEXT NOT NULL REFERENCES session_repos(repo_path)",
         "repo_path TEXT REFERENCES session_repos(repo_path)"),
        ("base_ref TEXT NOT NULL CHECK (length(base_ref) > 0)",
         "base_ref TEXT CHECK (base_ref IS NULL OR length(base_ref) > 0)"),
    )
    for old, new in swaps:
        if sql.count(old) != 1:
            raise RuntimeError(f"migration 20: unexpected agent_sessions schema ({old!r})")
        sql = sql.replace(old, new)
    end = sql.rstrip().rfind(")")
    modes = ",".join(f"'{m}'" for m in _BROWSER_MODES)
    sql = (sql[:end].rstrip()
           + f", browser_mode TEXT NOT NULL DEFAULT 'off' CHECK (browser_mode IN ({modes})),"
           " CHECK ((browser_mode = 'off') = (repo_path IS NOT NULL)),"
           " CHECK (repo_path IS NULL OR base_ref IS NOT NULL))")
    sql = sql.replace("CREATE TABLE agent_sessions", "CREATE TABLE agent_sessions__v20", 1)
    sql = sql.replace("CREATE TABLE IF NOT EXISTS agent_sessions", "CREATE TABLE agent_sessions__v20", 1)
    if "agent_sessions__v20" not in sql:
        raise RuntimeError("migration 20: could not rename agent_sessions")
    indexes = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'agent_sessions' "
        "AND sql IS NOT NULL")]
    collist = ", ".join(f'"{c}"' for c in cols)
    # SQLite's documented table-rebuild procedure: foreign_keys OFF (only
    # possible outside a transaction, so earlier migrations of this run are
    # committed first), rebuild in one transaction, foreign_key_check, restore.
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("SAVEPOINT mig20")
        try:
            conn.execute("DROP TABLE IF EXISTS agent_sessions__v20")
            conn.execute(sql)
            conn.execute(f"INSERT INTO agent_sessions__v20 ({collist}) "
                         f"SELECT {collist} FROM agent_sessions")
            conn.execute("DROP TABLE agent_sessions")
            conn.execute("ALTER TABLE agent_sessions__v20 RENAME TO agent_sessions")
            for idx in indexes:
                conn.execute(idx)
            broken = [r for r in conn.execute("PRAGMA foreign_key_check")
                      if r[2] == "agent_sessions"]
            if broken:
                raise RuntimeError(f"migration 20: {len(broken)} dangling session references")
            conn.execute("RELEASE mig20")
        except Exception:
            conn.execute("ROLLBACK TO mig20")
            conn.execute("RELEASE mig20")
            raise
    finally:
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys = ON")


def _mig_21_documents_project_key(conn: sqlite3.Connection) -> None:
    """documents.project_key — the business a document belongs to.

    Until now the RAG searched every document of every project, so a
    question about one business answered with another business's files (and
    cited them). 2026-10-23.
    """
    row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                       "AND name = 'documents'").fetchone()
    if row is None:
        return  # no documents table in this DB (partial install): nothing to do
    cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)")]
    if "project_key" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN project_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_project_key "
                 "ON documents(project_key)")


MIGRATIONS.append((20, "browser sessions: agent_sessions.browser_mode, repo optional (B11)",
                   _mig_20_browser_sessions))
def _mig_22_mcp_server_trust(conn: sqlite3.Connection) -> None:
    """mcp_servers.trust_readonly — whether to believe this server's own
    read-only annotations.

    The product promises that a tool is read-only by default, so a server
    the user chose to run is trusted to label its own tools. What it is not
    trusted to do is change its mind: when a tool's description or
    annotations change, the trust is revoked and the tool goes back through
    the approval gate until the user has looked at it. 2026-10-23.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(mcp_servers)")]
    if "trust_readonly" not in cols:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                           "AND name = 'mcp_servers'").fetchone()
        if row is None:
            return  # connectors framework not installed in this DB
        conn.execute("ALTER TABLE mcp_servers ADD COLUMN trust_readonly INTEGER NOT NULL DEFAULT 1")
    # The servers that were already installed were trusted under the old
    # behaviour; they stay trusted.
    conn.execute("UPDATE mcp_servers SET trust_readonly=1")


MIGRATIONS.append((21, "documents.project_key: RAG isolated per project (2026-10-23)",
                   _mig_21_documents_project_key))
MIGRATIONS.append((22, "mcp_servers.trust_readonly: MCP write gating (2026-10-23)",
                   _mig_22_mcp_server_trust))


def _mig_23_media_providers(conn: sqlite3.Connection) -> None:
    """media_providers — who does the work, and with what credentials.

    The media engine used to be one vendor. Now a provider declares the
    capabilities it serves and this table holds its configuration, so the
    product can ask for "an image" without knowing whether it comes from a
    cloud API, a local ComfyUI or an OpenAI-compatible server on this Mac.
    2026-10-23.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS media_providers (
               key TEXT PRIMARY KEY,
               enabled INTEGER NOT NULL DEFAULT 0,
               priority INTEGER NOT NULL DEFAULT 100,
               config_json TEXT NOT NULL DEFAULT '{}',
               last_error TEXT,
               checked_at INTEGER,
               updated_at INTEGER NOT NULL
           )""")


MIGRATIONS.append((23, "media_providers: vendor-agnostic media backends (2026-10-23)",
                   _mig_23_media_providers))


def _mig_24_media_provider_usage(conn: sqlite3.Connection) -> None:
    """media_provider_usage — who did the work, how often, and what it cost.

    A provider that silently burns money is worse than one that refuses:
    the cap is the brake, and the counters are how the user sees it coming.
    2026-10-23.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS media_provider_usage (
               provider_key TEXT NOT NULL,
               day TEXT NOT NULL,
               calls INTEGER NOT NULL DEFAULT 0,
               errors INTEGER NOT NULL DEFAULT 0,
               updated_at INTEGER NOT NULL,
               PRIMARY KEY (provider_key, day)
           )""")


MIGRATIONS.append((24, "media_provider_usage: per-provider counters and daily caps (2026-10-23)",
                   _mig_24_media_provider_usage))


# ── 25: local results of live evals (learning design §3.2/§4, PR L2) ────────
# The design numbered these tables "12"; 9-15 and 20 are taken and 16-19 are
# reserved for the OAuth branches, so per D15 they land after the last taken
# number: 25 here (21-24 are used by other migrations). Additive and
# idempotent (GAP_FILL_FROM). Only ``python -m ops_dashboard.evals run --live``
# writes here, on the user's own machine. ``key_ref`` is the keychain item
# NAME (never the key); no prompt or answer text is stored, only check results.
def _mig_25_eval_runs(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS eval_runs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  mode TEXT NOT NULL CHECK (mode IN ('offline','live')),"
        "  suite TEXT NOT NULL,"
        "  provider TEXT, model TEXT, key_ref TEXT,"
        "  budget_cost_cents REAL,"
        "  cost_cents_est REAL NOT NULL DEFAULT 0,"
        "  max_calls INTEGER, calls INTEGER NOT NULL DEFAULT 0,"
        "  passed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,"
        "  not_run INTEGER NOT NULL DEFAULT 0,"
        "  status TEXT NOT NULL DEFAULT 'running'"
        "    CHECK (status IN ('running','completed','stopped','failed')),"
        "  stop_reason TEXT,"
        "  started_at INTEGER NOT NULL, finished_at INTEGER, report_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS eval_results ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,"
        "  case_id TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'run' CHECK (status IN ('run','not_run')),"
        "  passed INTEGER NOT NULL,"
        "  checks TEXT NOT NULL DEFAULT '[]',"
        "  tokens_in INTEGER, tokens_out INTEGER,"
        "  usage_estimated INTEGER NOT NULL DEFAULT 0,"
        "  cost_cents_est REAL NOT NULL DEFAULT 0,"
        "  duration_ms INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id)")


MIGRATIONS.append((25, "eval_runs + eval_results: local live-eval results (L2)", _mig_25_eval_runs))


# ── 26: retire the leftover billing tables (the license/billing code left in
# the previous change). ConductLoop is free; nothing reads or writes these three
# tables any more. DESTRUCTIVE for local data: every row in them is deleted.
# Idempotent (DROP TABLE IF EXISTS; GAP_FILL_FROM) and it touches no other table
# (the indexes of the three tables are dropped together with them). The number
# is 26 because 25 is taken by the live-eval tables; the runner applies versions
# that land out of order, so either may ship first.
def _mig_26_drop_billing_tables(conn: sqlite3.Connection) -> None:
    for table in ("entitlements", "founders", "payment_events"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


MIGRATIONS.append((26, "drop entitlements, founders, payment_events (billing retired; deletes local rows)",
                   _mig_26_drop_billing_tables))
