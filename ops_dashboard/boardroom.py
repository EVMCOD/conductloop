"""Boardroom API helpers (created 2026-07-01).

Chairman-only control panel for managing the AI C-Suite. Routes registered
in server.py delegate here. Functions are defensive — wrap DB calls in try/except
so a transient issue doesn't 500 the whole inbox.
"""
from __future__ import annotations

import json
import os
import time

from . import db

# --- Authorization --------------------------------------------------------

def _is_chairman_user(user: dict | None) -> bool:
    if not user:
        return False
    # Chairman flag is the source of truth; is_admin is a secondary signal so
    # the test admin user can also experiment locally without a second login.
    if int(user.get("is_chairman") or 0) == 1:
        return True
    if int(user.get("is_admin") or 0) == 1:
        return True
    return False


def require_chairman(user: dict | None) -> tuple[bool, str | None]:
    """Return (ok, error_message). Use at the top of boardroom handlers."""
    if not user:
        return False, "unauthorized"
    if not _is_chairman_user(user):
        return False, "chairman only"
    return True, None


def current_chairman_id() -> int | None:
    """Return chairman_user_id from boardroom_settings, or None."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT chairman_user_id FROM boardroom_settings WHERE id = 1").fetchone()
    return int(row["chairman_user_id"]) if row else None


# --- Inbox ----------------------------------------------------------------

_STATUS_BADGES = {
    "idle":     ("🟢", "idle"),
    "working":  ("🟢", "working"),
    "waiting":  ("🟡", "waiting"),
    "blocked":  ("🔴", "blocked"),
    "paused":   ("⚪", "paused"),
    "active":   ("🟢", "active"),
}


def _badge_for(team_status: str | None) -> tuple[str, str]:
    return _STATUS_BADGES.get((team_status or "").lower(), ("⚪", team_status or "unknown"))


def inbox() -> dict:
    """Return the chairman's inbox: list of all CEOs (incl. Holding) with last activity."""
    items: list[dict] = []
    with db.get_connection() as conn:
        ceos = conn.execute(
            "SELECT id, name, role, level, capabilities, status, status_detail, "
            "       project_slug, last_active_at "
            "  FROM team_members "
            " WHERE is_ceo = 1 "
            " ORDER BY (level = 'ceo') DESC, level, name COLLATE NOCASE"
        ).fetchall()
        for c in ceos:
            ceo_id = c["id"]
            icon, label = _badge_for(c["status"])
            # Last message preview (may be empty)
            last_msg_row = conn.execute(
                "SELECT role, content, created_at "
                "  FROM ceo_messages WHERE member_id = ? "
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (ceo_id,),
            ).fetchone()
            last_msg = dict(last_msg_row) if last_msg_row else None
            # Last 24h activity count
            day_ago = int(time.time()) - 86400
            events = conn.execute(
                "SELECT COUNT(*) AS n FROM activity_feed "
                " WHERE created_at >= ? AND summary LIKE ?",
                (day_ago, f"%{c['name']}%"),
            ).fetchone()
            items.append({
                "id": ceo_id,
                "name": c["name"],
                "role": c["role"] or c["level"] or "ceo",
                "level": c["level"],
                "capabilities": c["capabilities"] or "",
                "project_slug": c["project_slug"],
                "status": (c["status"] or "idle").lower(),
                "status_label": label,
                "status_icon": icon,
                "status_detail": c["status_detail"],
                "last_active_at": c["last_active_at"],
                "last_message": (
                    {
                        "role": last_msg["role"],
                        "content": (last_msg["content"] or "")[:140],
                        "created_at": last_msg["created_at"],
                    } if last_msg else None
                ),
                "events_24h": int(events["n"]) if events else 0,
            })
    return {"items": items, "ts": int(time.time())}


def ceo_detail(ceo_id: int) -> dict | None:
    """Return CEO thread: member row + autonomy config + last N messages."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id, name, role, level, type, capabilities, status, status_detail, "
            "       project_slug, last_active_at, model, notes "
            "  FROM team_members WHERE id = ? AND is_ceo = 1",
            (ceo_id,),
        ).fetchone()
        if not row:
            return None
        autonomy = conn.execute(
            "SELECT default_tier, maturity_stage, per_action, require_chairman "
            "  FROM autonomy_config WHERE member_id = ?",
            (ceo_id,),
        ).fetchone()
        msgs = conn.execute(
            "SELECT id, role, content, created_at, user_id "
            "  FROM ceo_messages WHERE member_id = ? "
            " ORDER BY created_at ASC, id ASC LIMIT 200",
            (ceo_id,),
        ).fetchall()
    out = dict(row)
    out["autonomy"] = dict(autonomy) if autonomy else {
        "default_tier": 2,
        "maturity_stage": "traction",
        "per_action": "{}",
        "require_chairman": 0,
    }
    out["messages"] = [dict(m) for m in msgs]
    if autonomy and isinstance(autonomy["per_action"], str):
        try:
            out["autonomy"]["per_action_obj"] = json.loads(autonomy["per_action"])
        except Exception:
            out["autonomy"]["per_action_obj"] = {}
    return out


def append_ceo_message(member_id: int, user_id: int | None, role: str, content: str) -> dict:
    """Append a message to a CEO's thread. Returns the saved row.
    If role='chairman' and the content contains a status-pivot keyword
    ('approval', 'wait for you', 'go ahead', etc.), the CEO's status field
    is updated automatically so the Inbox badge reflects reality without
    requiring the chairman to click anything."""
    if role not in ("chairman", "ceo", "system"):
        role = "system"
    now = int(time.time())
    status_pivot = None if role != "chairman" else _detect_chairman_intent(content)
    old_status = None
    new_status = None
    member_name = None
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO ceo_messages (member_id, user_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (member_id, user_id, role, content, now),
        )
        # Read old status BEFORE updating so we can detect the transition.
        pre = conn.execute(
            "SELECT status, name FROM team_members WHERE id = ?", (member_id,)
        ).fetchone()
        old_status = (pre["status"] if pre else None) or "idle"
        member_name = (pre["name"] if pre else None) or f"#{member_id}"
        # Bump the CEO's last_active_at so the inbox badge reflects activity.
        # If status_pivot was detected, override the status field.
        if status_pivot:
            conn.execute(
                "UPDATE team_members SET last_active_at = ?, status = ?, status_detail = ? WHERE id = ?",
                (now, status_pivot["status"], status_pivot["detail"][:200], member_id),
            )
            new_status = status_pivot["status"]
        else:
            conn.execute(
                "UPDATE team_members SET last_active_at = ?, status = CASE WHEN status = 'idle' THEN 'working' ELSE status END "
                "WHERE id = ?",
                (now, member_id),
            )
            # Re-read to capture the post-update status
            post = conn.execute(
                "SELECT status FROM team_members WHERE id = ?", (member_id,)
            ).fetchone()
            new_status = (post["status"] if post else None) or "idle"
        conn.commit()
        msg_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, member_id, user_id, role, content, created_at FROM ceo_messages WHERE id = ?",
            (msg_id,),
        ).fetchone()
    # Out-of-band: notify Discord bridge on status transitions. Wrapped so
    # a broken bridge never breaks the message persist path.
    if role == "chairman" and new_status != old_status:
        try:
            from . import discord_bridge
            discord_bridge.broadcast_status_change(member_id, old_status, new_status, member_name)
        except Exception:
            pass
    return dict(row) if row else {}


# CEO auto-reply (background task). After chairman posts a message, a daemon
# thread invokes the LLM with the CEO's persona + recent thread and persists
# the response back to ceo_messages. Mock provider returns a canned line so
# the UI flow works without a BYOK key.

def generate_ceo_reply(member_id: int, trigger_msg_id: int | None = None) -> dict | None:
    """Synthesize a CEO reply. Called as background task. Returns the saved
    row (None on failure). Marks the CEO's status as 'working' / 'replied'."""
    try:
        ceo = ceo_detail(member_id)
        if not ceo:
            return None
        ceo_name = ceo.get("name") or "CEO"
        project = ceo.get("project_slug") or ""
        persona = ceo.get("capabilities") or "experienced CEO"
        autonomy = ceo.get("autonomy", {})
        thread = ceo.get("messages", [])[-12:]  # last 12 turns
        tier = autonomy.get("default_tier") or 2

        system_prompt = (
            f"You are {ceo_name}, a project CEO in {_owner()}'s AI C-Suite."
            + (f" Project: {project}." if project else "")
            + (f" Persona: {persona}" if persona else "")
            + f" Default autonomy tier: {tier}."
            + "\n\nREPLY RULES (strict):\n"
            "1. Reply to the chairman's most recent message. Max 3 sentences.\n"
            "2. NUMERIC METRICS are sacred. NEVER invent percentage, count, or dollar figures.\n"
            "   If you don't have data: say 'no data', 'I haven't measured this', "
            "   or 'need to check'. Don't fabricate.\n"
            "3. NEVER invent URLs, emails, usernames, dates, or named resources.\n"
            "4. If the chairman asks about a topic outside your project scope (e.g. cross-"
            "   project revenue, other CEOs' status), ANSWER HONESTLY within your limits, "
            "   or ask the chairman to check with the Holding CEO.\n"
            "5. Never start with 'I', never greet, never sign off. Just answer.\n\n"
            "EXAMPLES (don't be like the bad ones):\n"
            "- BAD: 'Open rate dropped 38%, our weakest channel is LinkedIn'\n"
            "  GOOD: 'No channel-level analytics in the data — check GA per-site before ranking.'\n"
            "- BAD: 'We have 47 active users, $4,250 MRR'\n"
            "  GOOD: 'Recent MRR per revenue_events is $0 this week; want me to run a GA pull?'\n"
        )
        convo_lines = ["Conversation so far (last 12 messages):\n"]
        for m in thread:
            who = "Chairman" if m.get("role") == "chairman" else (ceo_name if m.get("role") == "ceo" else "System")
            convo_lines.append(f"{who}: {(m.get('content') or '')[:280]}\n")
        # Add minimal real-analytics block (per-project only, not global)
        try:
            with db.get_connection() as conn:
                rev = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total "
                    "  FROM revenue_events "
                    " WHERE event_type = 'payment_succeeded' AND occurred_at >= ? AND project_key = ?",
                    (int(time.time()) - 7 * 86400, project or ""),
                ).fetchone()
                urldiff = conn.execute(
                    "SELECT COUNT(*) AS ok FROM live_urls WHERE ok = 1"
                ).fetchone()
                convo_lines.append(
                    f"\nREAL ANALYTICS (last 7d):\n"
                    f"- {project or 'this project'}: {int(rev['n'] or 0)} payment events, "
                    f"{(int(rev['total'] or 0) / 100.0):.2f} USD\n"
                    f"- Live URLs health: {int(urldiff['ok'] or 0)} up (across all monitored sites)\n"
                )
        except Exception:
            pass
        convo_lines.append("\nWrite your reply now (max 3 sentences, no fabrication).")
        user_prompt = "".join(convo_lines)

        # LLM call (or graceful fallback chain: configured provider → ollama → mock → canned)
        reply_text = None
        provider_name = None
        try:
            from . import brain
            configured = brain.resolve_provider()
            # Try the configured provider first. If it fails with a transient
            # error (auth, network, 5xx), fall through to ollama if available,
            # else to a canned line. Never silent-fail to a static line on
            # the first attempt — surface that the configured provider raised
            # but continue with whatever fallback succeeds.
            try:
                if configured.name == "mock":
                    reply_text = (
                        f"Noted. Acting within Tier {tier} authority. I'll follow up when I have something concrete."
                    )
                    provider_name = "mock"
                else:
                    reply_text = configured.ask(system_prompt, [{"role": "user", "content": user_prompt}])
                    provider_name = configured.name
            except Exception as e_first:
                # Try ollama as a graceful fallback so the CEO doesn't go silent.
                # We intentionally do NOT prepend the provider error to the reply
                # text — the chairman sees provider_name via the UI badge, and the
                # raw text should read like a real CEO reply. (This mirrors the
                # Holding CEO synthesis fallback at the top of this file.)
                try:
                    fallback = brain.resolve_provider(name="ollama")
                    reply_text = fallback.ask(system_prompt, [{"role": "user", "content": user_prompt}])
                    # Label the fallback cause so the UI can surface it (insufficient
                    # balance vs invalid key vs unknown), matching the Holding CEO
                    # synthesis pattern.
                    e_str = str(e_first).lower() if isinstance(e_first, Exception) else ""
                    if "insufficient balance" in e_str or "402" in e_str:
                        cause = f"{configured.name}_insufficient_balance_402"
                    elif "invalid api key" in e_str or "401" in e_str:
                        cause = f"{configured.name}_invalid_key_401"
                    else:
                        cause = f"{configured.name}_{type(e_first).__name__}"
                    provider_name = f"ollama-from-{cause}"
                except Exception as e_fb:
                    # No fallback available either — short, honest error reply
                    reply_text = (
                        f"My primary LLM ({configured.name}) raised "
                        f"{type(e_first).__name__}: {str(e_first)[:60]}; "
                        f"the local fallback also failed ({type(e_fb).__name__}). "
                        f"Set BRAIN_PROVIDER=ollama or restore the {configured.name} key."
                    )
                    provider_name = f"error:{type(e_first).__name__}+{type(e_fb).__name__}"
        except Exception:
            reply_text = f"No LLM provider configured. Standing by (Tier {tier})."
            provider_name = "fallback"

        if not reply_text or len(reply_text.strip()) < 2:
            reply_text = "No reply available right now. Ping me again later."

        clean = reply_text.strip()
        # Don't reply-to-self loops: if the LLM asked a question, persist it as
        # a ceo message; the chairman sees it next refresh / via SSE.
        msg = append_ceo_message(member_id, None, "ceo", clean)
        # Update CEO status: working + replied
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE team_members SET status = ?, status_detail = ?, last_active_at = ? WHERE id = ?",
                ("working", f"replied via {provider_name or 'provider'}", int(time.time()), member_id),
            )
            conn.commit()
        # Auto-publish to bound channels (telegram / discord). Best-effort;
        # never raise — a misconfigured webhook should never block the reply.
        try:
            from . import ceo_channels as _channels
            publish = _channels.publish_outbound(member_id, clean)
            # Attach the publish summary to the message so the chairman can see
            # which channels got it (visible in the thread view).
            if publish.get("deliveries"):
                parts = []
                for d in publish["deliveries"]:
                    status = "OK" if d["ok"] else str(d.get("error") or "error")
                    parts.append(f"{d['platform']}={d['target_id']}:{status}")
                summary = ", ".join(parts)
                append_ceo_message(
                    member_id, None, "system",
                    f"(auto-published to {len(publish['deliveries'])} channel(s): {summary})"
                )
        except Exception:
            pass
        return msg
    except Exception as e:
        # Always log a system row so the chairman sees the failure
        try:
            append_ceo_message(member_id, None, "system", f"(ceo reply crashed: {type(e).__name__}: {str(e)[:120]})")
        except Exception:
            pass
        return None


def spawn_ceo_reply(member_id: int, trigger_msg_id: int | None = None) -> bool:
    """Spawn a background thread that generates the CEO reply. Returns True
    on success. Failures are logged to ceo_messages as system rows."""
    import threading
    def run():
        try:
            generate_ceo_reply(member_id, trigger_msg_id)
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return True


# Status intent detection — parses chairman messages and infers status transitions
# so the Inbox badge stays in sync with reality. Pure keyword matching (no LLM)
# keeps it fast and deterministic. The detail field on team_members surfaces the
# reason so the chairman can see why a CEO flipped states.

_STATUS_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # Ordered most-urgent first. We pick the first match in declaration order
    # so 'blocked' wins over 'waiting' wins over 'paused' wins over 'working'.
    # Words are substring-matched case-insensitively (no word boundaries),
    # so ambiguous present-tense 'approve' is intentionally omitted to avoid
    # the false positive that flipped 'approved!' into 'waiting'.
    ("waiting", ("approval", "approval needed", "needs decision", "need decision",
                 "wait for you", "needs you", "your call", "esperando",
                 "necesito decisión", "requires approval",
                 "go/no-go", "go no go", "sign-off", "sign off")),
    ("blocked", ("blocked on", "blocked by", "blocked", "bloqueado",
                 "can't proceed", "cannot proceed", "stuck", "no puedo avanzar",
                 "incident")),
    ("working", ("approved", "go ahead", "proceed", "ship it",
                 "approved,", "approved.", "approved!",
                 "launch", "go.", "adelante", "procede", "sigue", "continue")),
    ("paused", ("pause", "paused", "on hold", "hold", "cancel", "stop")),
]


def _detect_chairman_intent(content: str) -> dict | None:
    """Parse the chairman message and return {status, detail} if a status-pivot
    keyword is detected. Returns None when no clear intent (default behavior).

    Strategy: walk _STATUS_KEYWORDS in declaration order and return on first
    match. That's enough — the keywords are tuned so ambiguous ones ('approve'
    without '-d' / '-al') are absent."""
    if not content:
        return None
    text = content.lower()
    for status, kws in _STATUS_KEYWORDS:
        for kw in kws:
            if kw in text:
                return {
                    "status": status,
                    "detail": f"chairman: '…{kw}…' detected",
                }
    return None


# --- Wizard: create a new CEO --------------------------------------------

# Tool suggestions keyed by project slug — the wizard uses these as defaults.
# Built in: only the generic ``holdco``. Your own projects go in config.json:
#   {"boardroom": {"tool_defaults_by_project": {"my-app": ["publish_blog_post"]}}}
# (a config entry replaces the built-in one for that slug; unknown tool names
# and values of the wrong type are ignored).
_TOOL_DEFAULTS_BY_PROJECT: dict[str, list[str]] = {
    "holdco": ["read_analytics_source", "read_finance_report", "schedule_chairman_brief"],
}
_DEFAULT_TOOLS = ["read_file", "write_file", "search_vault", "write_to_vault", "send_message", "request_chairman_decision"]


def _configured_tool_defaults() -> dict[str, list[str]]:
    """``boardroom.tool_defaults_by_project`` from config.json, validated."""
    from . import app_paths

    try:
        raw = json.loads(app_paths.config_path().read_text(encoding="utf-8"))
        cfg = raw["boardroom"]["tool_defaults_by_project"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    known = {t["name"] for t in all_tool_catalog()}
    out: dict[str, list[str]] = {}
    for slug, tools in cfg.items():
        if isinstance(slug, str) and isinstance(tools, list):
            out[slug] = [t for t in tools if isinstance(t, str) and t in known]
    return out


def suggest_tools_for_project(slug: str | None) -> list[str]:
    defaults = {**_TOOL_DEFAULTS_BY_PROJECT, **_configured_tool_defaults()}
    if slug and slug in defaults:
        return list(_DEFAULT_TOOLS) + list(defaults[slug])
    return list(_DEFAULT_TOOLS)


def all_tool_catalog() -> list[dict]:
    """Return the static catalog of tools the wizard shows."""
    return [
        {"name": "read_file",                    "category": "base", "description": "Read a file from the vault or a repo."},
        {"name": "write_file",                   "category": "base", "description": "Write a file to disk."},
        {"name": "search_vault",                 "category": "base", "description": "Full-text search across the indexed vault."},
        {"name": "write_to_vault",               "category": "base", "description": "Append a markdown entry to the vault."},
        {"name": "send_message",                 "category": "base", "description": "Send an outbound message (Slack/Discord/SMS)."},
        {"name": "request_chairman_decision",    "category": "base", "description": "Escalate a choice to the chairman in the Inbox."},
        {"name": "publish_mql5_script",          "category": "community", "description": "Publish an MQL5 script to community."},
        {"name": "publish_tradingview_script",   "category": "community", "description": "Publish a TradingView indicator/pine."},
        {"name": "post_to_discord",              "category": "community", "description": "Post a message in the Discord channel."},
        {"name": "schedule_tweet",               "category": "community", "description": "Schedule a tweet for later posting."},
        {"name": "read_analytics_source",        "category": "analytics",        "description": "Pull GA4 / Sentry summary."},
        {"name": "draft_outreach_email",         "category": "marketing",       "description": "Draft an outreach email for chairman approval."},
        {"name": "publish_blog_post",            "category": "marketing",       "description": "Publish a post on the marketing site."},
        {"name": "monitor_signups",              "category": "marketing",       "description": "Watch the waitlist for new signups."},
        {"name": "monitor_support_threads",      "category": "support",         "description": "Watch the support inbox."},
        {"name": "draft_release_notes",          "category": "engineering",     "description": "Draft release notes for a version."},
        {"name": "read_finance_report",          "category": "finance",         "description": "Pull the portfolio revenue forecast."},
        {"name": "schedule_chairman_brief",      "category": "holdco",          "description": "Queue a Daily Recap entry for chairman."},
    ]


def create_ceo(
    *,
    name: str,
    project_slug: str | None,
    persona_summary: str,
    tier: int = 2,
    maturity: str = "traction",
    tools: list[str] | None = None,
    chairman_user_id: int | None = None,
) -> dict:
    """Persist a new team_member as CEO + autonomy_config + decision audit row."""
    if not name or not name.strip():
        raise ValueError("name is required")
    name = name.strip()
    persona_summary = persona_summary.strip() or "30-year VC mindset. Reliable, brief, decisive."
    tier = max(1, min(4, int(tier)))
    if maturity not in {"chaotic", "traction", "scale", "mature", "pause"}:
        maturity = "traction"
    tools = tools or []
    now = int(time.time())

    # The chairman's team owns new CEOs.
    with db.get_connection() as conn:
        team_id = _chairman_team_id(conn)
        if team_id is None:
            raise ValueError("no teams table seed found")

        # Avoid name collisions; suffix ` (n)` if needed
        final_name = name
        n = 2
        while conn.execute("SELECT 1 FROM team_members WHERE name = ?", (final_name,)).fetchone():
            final_name = f"{name} ({n})"
            n += 1

        cur = conn.execute(
            "INSERT INTO team_members "
            "(team_id, name, role, type, level, capabilities, model, notes, "
            " created_at, updated_at, is_ceo, status, status_detail, project_slug, last_active_at) "
            "VALUES (?, ?, 'ceo', 'ai_agent', 'ceo', ?, 'minimax-portal/MiniMax-M3', ?, ?, ?, 1, 'idle', ?, ?, ?)",
            (
                team_id,
                final_name,
                persona_summary,
                f"Created by Boardroom on {time.strftime('%Y-%m-%d')}. Persona: {persona_summary[:120]}",
                now, now,
                f"Tools: {len(tools)} enabled · tier {tier} · {maturity}",
                project_slug,
                now,
            ),
        )
        ceo_id = cur.lastrowid
        conn.execute(
            "INSERT INTO autonomy_config "
            "(member_id, default_tier, maturity_stage, per_action, require_chairman, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (ceo_id, tier, maturity, json.dumps({t: 1 for t in tools}), now),
        )
        # Audit log row
        if chairman_user_id is None:
            chairman_user_id = current_chairman_id()
        if chairman_user_id is not None:
            conn.execute(
                "INSERT INTO boardroom_decisions "
                "(user_id, member_id, kind, title, body, created_at) "
                "VALUES (?, ?, 'ceo_created', ?, ?, ?)",
                (
                    chairman_user_id,
                    ceo_id,
                    f"New CEO created: {final_name}",
                    json.dumps({
                        "project_slug": project_slug,
                        "tier": tier,
                        "maturity": maturity,
                        "tools_count": len(tools),
                        "persona": persona_summary[:280],
                    }, ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, role, level, status, project_slug, last_active_at "
            "  FROM team_members WHERE id = ?",
            (ceo_id,),
        ).fetchone()
    return dict(row) if row else {"id": ceo_id, "name": final_name}


# --- Autonomy config ------------------------------------------------------

def get_autonomy(member_id: int) -> dict | None:
    with db.get_connection() as conn:
        m = conn.execute("SELECT id, name FROM team_members WHERE id = ? AND is_ceo = 1", (member_id,)).fetchone()
        if not m:
            return None
        a = conn.execute(
            "SELECT default_tier, maturity_stage, per_action, require_chairman "
            "  FROM autonomy_config WHERE member_id = ?",
            (member_id,),
        ).fetchone()
    out = dict(m)
    out["autonomy"] = dict(a) if a else {
        "default_tier": 2, "maturity_stage": "traction", "per_action": "{}", "require_chairman": 0,
    }
    try:
        out["autonomy"]["per_action_obj"] = json.loads(out["autonomy"]["per_action"])
    except Exception:
        out["autonomy"]["per_action_obj"] = {}
    return out


# --- Daily Recap ----------------------------------------------------------

def recap() -> dict:
    """Daily Recap payload. Phase 1: pure DB rollup.
    Phase 2 (v1.0): embed synthesis via recap_synthesis()."""
    payload = _recap_payload()
    payload["synthesis"] = recap_synthesis(payload)
    return payload


def _recap_payload() -> dict:
    with db.get_connection() as conn:
        ceos = conn.execute(
            "SELECT id, name, role, level, status, status_detail, last_active_at, project_slug "
            "  FROM team_members WHERE is_ceo = 1 ORDER BY name"
        ).fetchall()
        recent_msgs = conn.execute(
            "SELECT cm.member_id, cm.role, cm.content, cm.created_at, tm.name AS ceo_name "
            "  FROM ceo_messages cm LEFT JOIN team_members tm ON tm.id = cm.member_id "
            " WHERE cm.created_at >= ? ORDER BY cm.created_at DESC LIMIT 12",
            (int(time.time()) - 86400 * 7,),
        ).fetchall()
        projects = conn.execute(
            "SELECT id, name, key FROM teams WHERE id NOT IN (SELECT DISTINCT team_id FROM team_members WHERE is_ceo=1) "
            "ORDER BY name LIMIT 30"
        ).fetchall()
    return {
        "generated_at": int(time.time()),
        "ceos": [dict(c) for c in ceos],
        "recent_messages": [dict(m) for m in recent_msgs],
        "projects": [dict(p) for p in projects],
        "kpi_rollup": [],
        "anomalies": [],
    }


def _owner() -> str:
    """Who the agents work for (OPS_OWNER_NAME); never a hardcoded person."""
    return os.environ.get("OPS_OWNER_NAME", "the operator").strip() or "the operator"


_HOLDING_CEO_PROMPT = (
    "You are the Holding CEO of a one-person AI holding company. The chairman "
    "({owner}) launched a Boardroom section where they manage multiple project "
    "CEOs. Every morning you synthesize a brief that opens with what needs their "
    "attention, then says what is OK.\n\n"
    "RULES (strict, every one applies):\n"
    "1. ONLY use the data below. Do NOT invent CEOs, projects, decisions, or metrics.\n"
    "2. If a metric is missing from the data, SAY SO EXPLICITLY ('no revenue data this "
    "week', 'no GA traffic recorded', 'unknown'). Never fabricate a number.\n"
    "3. Use exact CEO names and project names from the data.\n"
    "4. Lead with the blocked CEOs (highest priority). Then waiting CEOs (need a decision).\n"
    "5. Then 1-2 sentences about recent activity referencing real figures from the data block.\n"
    "6. End with one practical suggestion for today that references a real CEO or project.\n"
    "7. Match the chairman's language: lean English unless the recent_messages are "
    "predominantly Spanish.\n"
    "8. Max 6 lines. No greetings (\"Good morning\"), no signoff, no emojis unless the "
    "data uses them.\n\n"
    "EXAMPLES of bad vs good responses:\n"
    "- BAD: 'Open rates dropped 38%' (invented number).\n"
    "  GOOD: 'No email/channel analytics recorded this week.'\n"
    "- BAD: 'We have 47 active users' (invented count).\n"
    "  GOOD: 'GA recorded <N> users last week — see data block.'\n"
    "- BAD: 'LinkedIn is our weakest channel because engagement dropped.'\n"
    "  GOOD: 'No per-channel breakdown in the data; cannot rank channels yet.'\n"
)


def _static_synthesis(payload: dict) -> str:
    """Static fallback synthesis — same shape the LLM would produce, but DB-only."""
    blocked = [c for c in payload["ceos"] if (c.get("status") or "").lower() == "blocked"]
    waiting = [c for c in payload["ceos"] if (c.get("status") or "").lower() == "waiting"]
    total = len(payload["ceos"])
    lines = []
    if blocked:
        lines.append(f"🔴 {len(blocked)} CEO(s) blocked: {', '.join(c['name'] for c in blocked[:5])}.")
    if waiting:
        lines.append(f"🟡 {len(waiting)} CEO(s) waiting on a decision: {', '.join(c['name'] for c in waiting[:5])}.")
    if not blocked and not waiting:
        lines.append("🟢 No CEO is currently blocked or waiting.")
    lines.append(f"Active C-Suite: {total} CEO{'s' if total != 1 else ''}.")
    if payload.get("recent_messages"):
        last = payload["recent_messages"][0]
        preview = (last.get("content") or "")[:80]
        lines.append(f"Last activity: {last.get('ceo_name') or 'CEO'} — \"{preview}\".")
    lines.append("Tip: review blocked CEOs first; they cannot proceed without you.")
    return "\n".join(lines)


# The Holding-CEO synthesis runs on the HTTP request thread. A slow provider
# (a local Ollama model can generate for up to 120s) would otherwise make the
# /api/boardroom/recap request spin. Cap the LLM call at a wall-clock deadline
# and fall back to the static synthesis when it's exceeded.
_RECAP_LLM_TIMEOUT = float(os.environ.get("RECAP_LLM_TIMEOUT", "25"))


def _ask_with_deadline(provider, system: str, messages: list[dict], seconds: float) -> str:
    """Run ``provider.ask()`` under a wall-clock deadline. Raises ``TimeoutError``
    if it exceeds ``seconds``. The abandoned worker thread is not joined (we
    shut the executor down without waiting), so it ends on its own when the
    underlying HTTP call returns or hits the provider's own timeout."""
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(provider.ask, system, messages)
    try:
        return fut.result(timeout=seconds)
    except _cf.TimeoutError:
        raise TimeoutError(f"{getattr(provider, 'name', 'llm')} exceeded {seconds:.0f}s deadline")
    finally:
        ex.shutdown(wait=False)


def recap_synthesis(payload: dict | None = None, *, force: bool = False, ttl_seconds: int = 6 * 3600) -> dict:
    """Holding CEO synthesis with cache. Falls back to static if LLM unavailable.

    Returns: {text, source, provider, model, generated_at, cached, expires_at}.
    """
    if payload is None:
        payload = _recap_payload()
    # Stable hash of the input so we can invalidate on data drift
    payload_compact = {
        "ceos": payload.get("ceos", [])[:50],
        "recent_messages": payload.get("recent_messages", [])[:12],
        "projects": payload.get("projects", [])[:30],
    }
    payload_str = json.dumps(payload_compact, sort_keys=True, default=str)
    import hashlib
    payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()[:32]
    now = int(time.time())

    # Try cache first
    if not force:
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, synthesis, source, provider, model, generated_at, expires_at "
                "  FROM recap_cache "
                " WHERE kind = 'daily' AND payload_hash = ? AND expires_at > ? "
                " ORDER BY generated_at DESC LIMIT 1",
                (payload_hash, now),
            ).fetchone()
        if row:
            return {
                "text": row["synthesis"],
                "source": row["source"],
                "provider": row["provider"],
                "model": row["model"],
                "generated_at": row["generated_at"],
                "cached": True,
                "expires_at": row["expires_at"],
            }

    # Try LLM synthesis
    synthesis_text = None
    source = "static"
    provider_name = None
    model_name = None
    try:
        from . import brain  # lazy import (brain pulls providers on demand)
        configured = brain.resolve_provider()
        provider_name = configured.name
        if configured.name == "mock":
            # No real key configured → static
            synthesis_text = _static_synthesis(payload)
            source = "static"
        else:
            # Build a tight, structured digest. NO fabricated data.
            ceos = payload.get("ceos", [])
            blocked = sorted({c['name']: c for c in ceos if (c.get('status') or '').lower() == 'blocked'}.values(), key=lambda x: x['name'])
            waiting = sorted({c['name']: c for c in ceos if (c.get('status') or '').lower() == 'waiting'}.values(), key=lambda x: x['name'])
            idle = sorted({c['name']: c for c in ceos if (c.get('status') or '').lower() == 'idle'}.values(), key=lambda x: x['name'])
            msgs = payload.get("recent_messages", [])[:8]
            projects = payload.get("projects", [])
            digest = (
                "STRUCTURED DATA (use exactly this — do NOT invent):\n\n"
                f"CEOs total: {len(ceos)}\n"
                f"  Blocked ({len(blocked)}): {[(c['name'], c.get('status_detail') or '') for c in blocked]}\n"
                f"  Waiting ({len(waiting)}): {[(c['name'], c.get('status_detail') or '') for c in waiting]}\n"
                f"  Idle ({len(idle)}): {[c['name'] for c in idle[:10]]}\n"
                f"Recent messages (7d, newest first): {len(msgs)}\n"
            )
            for m in msgs:
                digest += f"  - [{m.get('ceo_name', '?')}] {m.get('role', '?')}: {(m.get('content') or '')[:140]}\n"
            digest += f"\nProjects on file: {len(projects)}\n"
            for p in projects[:8]:
                digest += f"  - {p.get('name')} ({p.get('key')})\n"
            # Live analytics from revenue + GA so the brief cites real numbers
            real_analytics = _gather_real_analytics()
            digest += "\n--- LIVE ANALYTICS (use these exact numbers, don't invent) ---\n"
            digest += real_analytics + "\n"
            digest += "\nNow write the brief following the rules in the system prompt."
            # Graceful fallback chain: configured → ollama → static
            answer = None
            fallback_used = None
            primary_err_label = None
            fallback_used = None
            try:
                answer = _ask_with_deadline(
                    configured,
                    _HOLDING_CEO_PROMPT.replace("{owner}", _owner()),
                    [{"role": "user", "content": digest}],
                    _RECAP_LLM_TIMEOUT,
                )
                if not answer or not str(answer).strip():
                    raise RuntimeError(f"{configured.name} returned empty answer")
            except Exception as e_first:
                # Classify the primary provider failure so the chairman can
                # tell apart 'insufficient balance' from 'invalid key' from
                # 'no auth header'. We keep this label even when the fallback
                # succeeds, so the UI can surface the real underlying cause.
                e_str = str(e_first).lower() if isinstance(e_first, Exception) else ""
                if "insufficient balance" in e_str or "402" in e_str:
                    primary_err_label = "minimax_insufficient_balance_402"
                elif "invalid api key" in e_str or "401" in e_str:
                    primary_err_label = "minimax_invalid_key_401"
                elif "1004" in e_str:
                    primary_err_label = "minimax_no_auth_header_1004"
                else:
                    primary_err_label = f"minimax_{type(e_first).__name__}"
                # On a timeout, skip the local retry: a slow model won't get
                # faster, so go straight to static. Only retry ollama for real
                # provider errors (auth / balance / network).
                if not isinstance(e_first, TimeoutError):
                    try:
                        fb = brain.resolve_provider(name="ollama")
                        answer = _ask_with_deadline(
                            fb,
                            _HOLDING_CEO_PROMPT.replace("{owner}", _owner()),
                            [{"role": "user", "content": digest}],
                            min(_RECAP_LLM_TIMEOUT, 15),
                        )
                        fallback_used = "ollama"
                    except Exception:
                        pass
                if answer is None:
                    provider_name = primary_err_label
                    raise  # let outer except handle static
            if fallback_used and primary_err_label:
                provider_name = f"{fallback_used}-from-{primary_err_label}"
            elif fallback_used:
                provider_name = f"{fallback_used}-fallback"
            if answer and len(answer.strip()) > 20:
                synthesis_text = answer.strip()
                source = "llm"
                # Try to get model name from config
                try:
                    cfg = brain.get_config()
                    model_name = cfg.get("models", {}).get(provider_name) if isinstance(cfg, dict) else None
                except Exception:
                    pass
    except Exception as e:
        # LLM unavailable — static fallback
        synthesis_text = _static_synthesis(payload)
        source = "static"
        provider_name = f"fallback:{type(e).__name__}"
        # Surface the actual error so we don't hide transient conditions
        # (auth errors, balance errors, etc.) behind a generic 'fallback:' label.
        if isinstance(e, Exception) and "insufficient balance" in str(e).lower():
            provider_name = "minimax_insufficient_balance_402"
        elif isinstance(e, Exception) and "invalid api key" in str(e).lower():
            provider_name = "minimax_invalid_key_401"
        elif isinstance(e, Exception) and "1004" in str(e):
            provider_name = "minimax_no_auth_header_1004"

    if synthesis_text is None or not synthesis_text.strip():
        synthesis_text = _static_synthesis(payload)
        source = "static"

    expires_at = now + ttl_seconds
    # Persist to cache
    try:
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO recap_cache (kind, payload_hash, payload, synthesis, source, provider, model, generated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "daily",
                    payload_hash,
                    payload_str,
                    synthesis_text,
                    source,
                    provider_name,
                    model_name,
                    now,
                    expires_at,
                ),
            )
            conn.commit()
    except Exception:
        pass

    return {
        "text": synthesis_text,
        "source": source,
        "provider": provider_name,
        "model": model_name,
        "generated_at": now,
        "cached": False,
        "expires_at": expires_at,
    }


# --- Vault (decisions log): directory + portfolio + vault facets ---

def vault_feed(limit: int = 50, kind: str | None = None, member_id: int | None = None, since: int | None = None) -> list[dict]:
    """Filterable vault feed. All filters optional; combined with AND."""
    where = []
    args = []
    if kind:
        where.append("bd.kind = ?"); args.append(kind)
    if member_id:
        where.append("bd.member_id = ?"); args.append(int(member_id))
    if since:
        where.append("bd.created_at >= ?"); args.append(int(since))
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT bd.id, bd.user_id, bd.member_id, bd.kind, bd.title, bd.body, bd.created_at, "
            f"       u.name AS user_name, tm.name AS ceo_name, tm.project_slug AS ceo_project "
            f"  FROM boardroom_decisions bd "
            f"  LEFT JOIN users u ON u.id = bd.user_id "
            f"  LEFT JOIN team_members tm ON tm.id = bd.member_id"
            f"{where_clause} ORDER BY bd.created_at DESC, bd.id DESC LIMIT ?",
            args,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["body_obj"] = json.loads(d.get("body") or "{}")
        except Exception:
            d["body_obj"] = None
        out.append(d)
    return out


def vault_kinds() -> list[dict]:
    """Distinct kinds with counts (for sidebar facets in vault browser)."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM boardroom_decisions GROUP BY kind ORDER BY n DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def directory() -> dict:
    """Table view of every CEO with autonomy tier, maturity, tools count, message stats."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT tm.id, tm.name, tm.role, tm.level, tm.status, tm.status_detail, "
            "       tm.capabilities, tm.project_slug, tm.last_active_at, tm.created_at, "
            "       a.default_tier, a.maturity_stage, a.per_action, a.require_chairman, "
            "       (SELECT COUNT(*) FROM ceo_messages WHERE member_id = tm.id) AS message_count, "
            "       (SELECT MAX(created_at) FROM ceo_messages WHERE member_id = tm.id) AS last_msg_at "
            "  FROM team_members tm LEFT JOIN autonomy_config a ON a.member_id = tm.id "
            " WHERE tm.is_ceo = 1 "
            " ORDER BY (tm.level='ceo') DESC, tm.level, tm.name COLLATE NOCASE",
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            pa = json.loads(d.get("per_action") or "{}")
            d["tools_count"] = len([k for k, v in pa.items() if v])
        except Exception:
            d["tools_count"] = 0
        items.append(d)
    return {"items": items, "ts": int(time.time())}


def _chairman_team_id(conn) -> int | None:
    """The chairman's (holding) team: OPS_CHAIRMAN_TEAM (a team key) if set and
    present, else the first team created."""
    key = os.environ.get("OPS_CHAIRMAN_TEAM", "").strip()
    row = conn.execute("SELECT id FROM teams WHERE key = ? LIMIT 1", (key,)).fetchone() if key else None
    if not row:
        row = conn.execute("SELECT id FROM teams ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def portfolio() -> dict:
    """Cross-project summary: each project team with its assigned CEO + last decision."""
    with db.get_connection() as conn:
        project_teams = conn.execute(
            "SELECT id, name, key, created_at FROM teams "
            " WHERE id IS NOT ? AND key NOT IN ('brain-squad','frontend-team','backend-team','marketing-team')"
            " ORDER BY name", (_chairman_team_id(conn),)
        ).fetchall()
        project_rows = []
        for p in project_teams:
            ceo = conn.execute(
                "SELECT id, name, status, last_active_at FROM team_members "
                " WHERE project_slug = ? AND is_ceo = 1 "
                " ORDER BY (level='ceo') DESC, id LIMIT 1",
                (p["key"],),
            ).fetchone()
            last_dec = conn.execute(
                "SELECT bd.id, bd.title, bd.created_at, bd.kind "
                "  FROM boardroom_decisions bd LEFT JOIN team_members tm ON tm.id = bd.member_id "
                " WHERE tm.project_slug = ? ORDER BY bd.created_at DESC LIMIT 1",
                (p["key"],),
            ).fetchone()
            project_rows.append({
                "team_id": p["id"],
                "name": p["name"],
                "key": p["key"],
                "ceo": dict(ceo) if ceo else None,
                "last_decision": dict(last_dec) if last_dec else None,
                "age_days": int((time.time() - (p["created_at"] or time.time())) / 86400),
            })
    return {
        "projects": project_rows,
        "kpis": {
            "projects_total": len(project_rows),
            "projects_with_ceo": sum(1 for p in project_rows if p["ceo"]),
            "projects_with_decisions": sum(1 for p in project_rows if p["last_decision"]),
        },
        "generated_at": int(time.time()),
    }


# --- Holding CEO chat reply (shared across bridges: Telegram, Discord) ---

def chat_with_holding_ceo(prompt: str) -> str:
    """Reply to the chairman from the Holding CEO over an external bridge
    (Telegram DMs, Discord channel, future channels). Single LLM call;
    uses brain.resolve_provider() so it picks up the same BYOK config as the
    rest of the Boardroom. On 'mock' provider we return a static fallback
    line so the bridge is functional even before the chairman sets a key."""
    from . import brain
    inbox_data = inbox()
    blocked = [c for c in inbox_data["items"] if (c.get("status") or "").lower() == "blocked"]
    waiting = [c for c in inbox_data["items"] if (c.get("status") or "").lower() == "waiting"]
    context = (
        f"State: {len(inbox_data['items'])} CEOs total. "
        f"{len(blocked)} blocked, {len(waiting)} waiting.\n"
    )
    if blocked:
        context += "Blocked: " + ", ".join(c["name"] for c in blocked[:5]) + "\n"
    if waiting:
        context += "Waiting: " + ", ".join(c["name"] for c in waiting[:5]) + "\n"
    # Attach the most recent synthesis as additional context (cache hit fast)
    try:
        syn = recap_synthesis(_recap_payload())
        context += "\nRecent brief:\n" + (syn.get("text") or "")[:600] + "\n"
    except Exception:
        pass

    system_prompt = (
        "You are the Holding CEO responding to the chairman's quick question from a chat bridge. "
        "Be terse — max 4 sentences. Plain text only: no markdown asterisks, no code fences, no emojis. "
        "Lead with the action item if there is one; otherwise state the status succinctly."
    )
    user_prompt = context + "\nChairman: " + (prompt or "").strip()

    try:
        provider = brain.resolve_provider()
        if provider.name == "mock":
            return (
                f"Boardroom is up. {len(inbox_data['items'])} CEOs active, "
                f"{len(blocked)} blocked, {len(waiting)} waiting. "
                "Set BRAIN_PROVIDER + BYOK key for richer replies."
            )
        answer = provider.ask(system_prompt, [{"role": "user", "content": user_prompt}])
        out = (answer or "").strip()
        return out or "(no answer)"
    except Exception as e:
        return f"(Holding CEO reply failed: {type(e).__name__})"


def _gather_real_analytics() -> str:
    """Pull last-7-day revenue + GA traffic from DB so the Holding CEO's
    synthesis cites real figures instead of fabricating them. Returns a
    multi-line plain-text block, ready to be inlined into the LLM digest."""
    import time
    now = int(time.time())
    week_ago = now - 7 * 86400
    two_weeks_ago = now - 14 * 86400
    lines = []

    with db.get_connection() as conn:
        # Revenue: count + sum by project, last 7d
        rev = conn.execute(
            "SELECT project_key, COUNT(*) AS n_events, COALESCE(SUM(amount_cents), 0) AS total_cents, currency "
            "  FROM revenue_events "
            " WHERE event_type = 'payment_succeeded' AND occurred_at >= ? "
            " GROUP BY project_key, currency "
            " ORDER BY total_cents DESC",
            (week_ago,),
        ).fetchall()
        if rev:
            lines.append("Revenue last 7d:")
            for r in rev:
                amount = (r['total_cents'] or 0) / 100.0
                lines.append(f"  - {r['project_key'] or 'unknown'}: {int(r['n_events'])} events, {amount:.2f} {r['currency'] or 'USD'}")
        else:
            lines.append("Revenue last 7d: no successful payments recorded")

        # Revenue compared to previous 7d (delta %)
        prev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total_cents "
            "  FROM revenue_events "
            " WHERE event_type = 'payment_succeeded' AND occurred_at >= ? AND occurred_at < ?",
            (two_weeks_ago, week_ago),
        ).fetchone()
        prev_total = (prev['total_cents'] or 0) / 100.0 if prev else 0
        cur_total = sum((r['total_cents'] or 0) / 100.0 for r in rev)
        if prev_total > 0 and cur_total > 0:
            delta_pct = ((cur_total - prev_total) / prev_total) * 100
            lines.append(f"Revenue WoW: {'+' if delta_pct >= 0 else ''}{delta_pct:.1f}% vs prior 7d")
        elif cur_total == 0 and prev_total == 0:
            lines.append("Revenue WoW: no data either window")
        elif cur_total > 0:
            lines.append(f"Revenue WoW: +{cur_total:.2f} USD (no prior period)")

        # GA: sessions+users last 14d per site
        ga = conn.execute(
            "SELECT site_id, project_key, date, sessions, users, page_views "
            "  FROM ga_metrics_daily WHERE date >= date('now','-14 days') "
            " ORDER BY date DESC LIMIT 28"
        ).fetchall()
        if ga:
            lines.append("GA traffic (last 14d, newest first):")
            for r in ga[:14]:
                rd = dict(r)
                lines.append(
                    f"  - site#{rd['site_id']} {(rd.get('project_key') or '?')}: {rd['date']} sessions={rd['sessions']} users={rd['users']} page_views={rd.get('page_views') or 0}"
                )
        else:
            lines.append("GA traffic last 14d: no data")

        # URL health (live URLs status)
        urls_ok = conn.execute(
            "SELECT COUNT(*) AS n FROM live_urls WHERE ok = 1"
        ).fetchone()
        urls_bad = conn.execute(
            "SELECT COUNT(*) AS n FROM live_urls WHERE ok = 0"
        ).fetchone()
        if urls_ok or urls_bad:
            lines.append(f"Live URLs health: {int(urls_ok['n'] or 0)} up, {int(urls_bad['n'] or 0)} down")

    if not lines:
        return "(no live analytics available yet)"
    return "\n".join(lines)
