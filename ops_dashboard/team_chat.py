"""Chat with the AI team (/chat).

Pick one agent and talk to it, or talk to the CEO in team mode: the CEO decides
who on the team should work on the request, those agents answer in parallel,
and the CEO writes the final answer from their work. Every step is emitted as
an event, so the page can show each agent working live (the multi-agent view
Grok uses).

Agents come from the Boardroom: AI members that are executives (``is_ceo`` or
level ceo/vp/director). A library without them gets a small built-in team, so
the feature works on a fresh install without writing anything to the database.

Events (one dict each, sent as SSE by routes_chat):
  start        {thread_id, agent, mode, provider, model}
  context      {citations}
  plan         {note, assignments: [{agent, name, title, avatar, task}]}
  agent_start  {agent}            agent_delta {agent, content}
  agent_done   {agent, ms}        agent_error {agent, message}
  synthesis    {}                 (the CEO starts the final answer)
  delta        {content}          (the answer the user reads)
  error        {message, code}
  done         {thread_id}
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections.abc import Iterator

from . import brain, db

MAX_MESSAGE_CHARS = 8000
MAX_ASSIGNMENTS = 4
AGENT_DEADLINE_SECONDS = 120      # whole parallel phase
PLAN_DEADLINE_SECONDS = 45
HISTORY_TURNS = 12
BRAIN_KEY = "brain"

_RULES = (
    "Rules:\n"
    "- Answer in the language of the user's latest message.\n"
    "- Use only facts from the context below or the conversation. Never invent numbers, "
    "names, URLs or dates; say which data is missing instead.\n"
    "- Be concrete and brief. Markdown is fine: short paragraphs and bullet lists.\n"
)

_AVATARS = [
    ("ceo", "🧭"), ("cto", "🛠️"), ("cmo", "📣"), ("cfo", "📊"), ("cso", "♟️"),
    ("product", "🧩"), ("design", "🎨"), ("sales", "🤝"), ("counsel", "⚖️"), ("legal", "⚖️"),
    ("cs", "💬"), ("devops", "⚙️"), ("mobile", "📱"), ("frontend", "🖥️"),
]

BUILTIN_TEAM = [
    {"key": "builtin:ceo", "name": "CEO", "role": "ceo", "title": "Chief Executive Officer",
     "skills": "priorities; strategy; trade-offs; delegation",
     "description": "Sets priorities, splits the work across the team and makes the call."},
    {"key": "builtin:cto", "name": "CTO", "role": "cto", "title": "Chief Technology Officer",
     "skills": "architecture; code review; technical risk; build vs buy",
     "description": "Architecture, code quality, technical risk and tooling choices."},
    {"key": "builtin:product", "name": "Head of Product", "role": "head_product", "title": "Head of Product",
     "skills": "user problems; scope; roadmap; prioritisation",
     "description": "What users need, what to build first and what to leave out."},
    {"key": "builtin:cmo", "name": "CMO", "role": "cmo", "title": "Chief Marketing Officer",
     "skills": "positioning; messaging; seo; content; launch",
     "description": "Positioning, messaging, SEO and launch plans."},
    {"key": "builtin:cfo", "name": "CFO", "role": "cfo", "title": "Chief Financial Officer",
     "skills": "pricing; costs; runway; unit economics",
     "description": "Pricing, costs, runway and unit economics."},
]

_BRAIN = {
    "key": BRAIN_KEY, "name": "Brain", "role": "brain", "title": "Your second brain",
    "avatar": "🧠", "skills": "notes; repos; memory; search",
    "description": "Answers from your notes, repos and memory, with sources.",
    "kind": "brain", "lead": False,
}


def _owner() -> str:
    return os.environ.get("OPS_OWNER_NAME", "").strip() or "the user"


def _avatar(role: str, name: str) -> str:
    probe = f"{role} {name}".lower()
    for needle, emoji in _AVATARS:
        if re.search(rf"\b{needle}\b|_{needle}\b|\b{needle}_", probe):
            return emoji
    return "✦"


def _member_agent(row: dict) -> dict:
    caps_raw = (row.get("capabilities") or "").strip()
    caps: dict = {}
    if caps_raw.startswith("{"):
        try:
            caps = json.loads(caps_raw)
        except ValueError:
            caps = {}
    description = caps.get("description") if caps else caps_raw
    notes = (row.get("notes") or "").strip()
    title = notes if notes and len(notes) <= 60 and not notes.lower().startswith("created by") else ""
    role = (row.get("role") or "agent").strip()
    if not title:
        title = role.replace("_", " ").upper() if len(role) <= 3 else role.replace("_", " ").title()
    return {
        "key": f"m:{row['id']}",
        "id": row["id"],
        "name": row.get("name") or "Agent",
        "role": role,
        "title": title,
        "level": row.get("level") or "",
        "avatar": caps.get("avatar") or _avatar(role, row.get("name") or ""),
        "skills": caps.get("skills") or "",
        "description": (description or "").strip(),
        "project": row.get("project_slug") or "",
        "model": row.get("model") or "",
        "manager_id": row.get("manager_id"),
        "kind": "member",
        "lead": False,
    }


def _stored_agents() -> list[dict]:
    try:
        with db.db() as conn:
            rows = conn.execute(
                "SELECT id, name, role, level, is_ceo, project_slug, model, capabilities, notes, manager_id "
                "FROM team_members WHERE type = 'ai_agent' "
                "AND (is_ceo = 1 OR level IN ('ceo', 'vp', 'director')) ORDER BY id"
            ).fetchall()
    except Exception:
        return []
    seen: set[str] = set()
    agents = []
    for r in rows:
        name = (r["name"] or "").strip().lower()
        if not name or name in seen:       # older seeds left duplicate copies
            continue
        seen.add(name)
        agents.append(_member_agent(dict(r)))
    return agents


def _pick_lead(agents: list[dict]) -> dict | None:
    ceos = [a for a in agents if a["role"] == "ceo"]
    holding = [a for a in ceos if not a["project"]]
    return (holding or ceos or [None])[0]


def roster() -> dict:
    """Agents for the chat page: Brain, then the lead CEO, then the team."""
    stored = _stored_agents()
    lead = _pick_lead(stored)
    if lead is None:
        team = [{**a, "avatar": _avatar(a["role"], a["name"]), "level": "", "project": "",
                 "model": "", "kind": "builtin", "lead": False} for a in BUILTIN_TEAM]
        team[0]["lead"] = True
        builtin = True
    else:
        lead = {**lead, "lead": True}
        team = [lead] + [a for a in stored if a["key"] != lead["key"]]
        builtin = False
    return {"agents": [dict(_BRAIN)] + team, "lead": team[0]["key"], "builtin_team": builtin}


def _find(agents: list[dict], key: str) -> dict | None:
    return next((a for a in agents if a["key"] == key), None)


def team_of(lead: dict, agents: list[dict]) -> list[dict]:
    """Who the lead can delegate to: its direct reports, else everyone else."""
    others = [a for a in agents if a["key"] not in (lead["key"], BRAIN_KEY)]
    if lead.get("kind") == "member":
        reports = [a for a in others if a.get("manager_id") == lead.get("id")]
        if reports:
            return reports
    return others


# ---------- persistence ----------

def _ensure_schema(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_threads ("
        " conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,"
        " agent_key TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'direct', created_at INTEGER NOT NULL)"
    )


def _new_thread(agent_key: str, mode: str, message: str) -> int:
    title = re.sub(r"\s+", " ", message).strip()[:80] or "chat"
    now = int(time.time())
    with db.db() as conn:
        _ensure_schema(conn)
        cur = conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
                           (title, now, now))
        tid = int(cur.lastrowid)
        conn.execute("INSERT INTO chat_threads (conversation_id, agent_key, mode, created_at) VALUES (?, ?, ?, ?)",
                     (tid, agent_key, mode, now))
        return tid


def _thread_agent(thread_id: int) -> str | None:
    with db.db() as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT agent_key FROM chat_threads WHERE conversation_id = ?", (thread_id,)).fetchone()
    return row[0] if row else None


def _save(thread_id: int, role: str, content: str, provider: str | None = None) -> None:
    db.add_message(thread_id, role, content, provider=provider)


def list_threads(agent_key: str | None = None, limit: int = 30) -> list[dict]:
    with db.db() as conn:
        _ensure_schema(conn)
        sql = ("SELECT c.id, c.title, c.updated_at, t.agent_key, t.mode FROM chat_threads t "
               "JOIN conversations c ON c.id = t.conversation_id")
        params: list = []
        if agent_key:
            sql += " WHERE t.agent_key = ?"
            params.append(agent_key)
        sql += " ORDER BY c.updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_thread(thread_id: int) -> dict | None:
    agent_key = _thread_agent(thread_id)
    if agent_key is None:
        return None
    with db.db() as conn:
        conv = conn.execute("SELECT id, title, updated_at FROM conversations WHERE id = ?", (thread_id,)).fetchone()
        rows = conn.execute("SELECT role, content, provider, created_at FROM messages "
                            "WHERE conversation_id = ? ORDER BY id", (thread_id,)).fetchall()
    return {"id": thread_id, "title": conv["title"] if conv else "", "agent": agent_key,
            "messages": [dict(r) for r in rows]}


def delete_thread(thread_id: int) -> bool:
    if _thread_agent(thread_id) is None:
        return False
    with db.db() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (thread_id,))
        conn.execute("DELETE FROM chat_threads WHERE conversation_id = ?", (thread_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (thread_id,))
    return True


def _history(thread_id: int) -> list[dict]:
    """Last user/assistant turns of a thread, oldest first (agent notes left out)."""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY id DESC LIMIT ?", (thread_id, HISTORY_TURNS)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ---------- prompts ----------

def _context(message: str) -> tuple[str, list[dict], str]:
    """Retrieval shared by every agent of a turn: (context, citations, extra rules)."""
    try:
        from .web_ask import guard_web_docs
        docs = brain._retrieve(message, k=6, project_key=brain._detect_project(message))
        docs, web_rules = guard_web_docs(docs)
        context, citations = brain._format_context(docs)
        return context, citations, web_rules
    except Exception:
        return "", [], ""


def _context_block(context: str, web_rules: str) -> str:
    return (f"\n\nRelevant context from {_owner()}'s notes and repos (cite with [N] when you use it):\n"
            + (context or "(no matches)") + web_rules)


def agent_system(agent: dict, context: str, web_rules: str, *, lead: dict | None = None) -> str:
    who = (f"You are {agent['name']}, {agent['title']} on {_owner()}'s team."
           + (f" {agent['description']}" if agent.get("description") else "")
           + (f"\nSkills: {agent['skills']}." if agent.get("skills") else ""))
    if lead is not None:
        how = (f"\n{lead['name']} (the CEO) assigned you a task from {_owner()}'s request. "
               "Work only on that task, from your own expertise, in under 200 words.\n")
    else:
        how = f"\nYou are talking directly with {_owner()}.\n"
    return who + how + _RULES + _context_block(context, web_rules)


def _team_lines(team: list[dict]) -> str:
    return "\n".join(f"- {a['key']}: {a['name']} — {a['title']}."
                     + (f" Skills: {a['skills']}." if a.get("skills") else "") for a in team)


def plan_system(lead: dict, team: list[dict]) -> str:
    return (
        f"You are {lead['name']}, CEO of {_owner()}'s team. The people you lead:\n{_team_lines(team)}\n\n"
        f"Decide who should work on {_owner()}'s latest message. Pick the 1 to 3 people whose expertise "
        f"matters most (up to {MAX_ASSIGNMENTS} only for broad, cross-functional requests). Pick nobody for "
        "small talk or a quick question you can answer alone. Give each person one concrete task, "
        "written to them, in the language of the user's message.\n"
        "Reply with JSON only, no prose and no code fences:\n"
        '{"note": "<one short sentence to the user on how you will handle it>", '
        '"assignments": [{"agent": "<key from the list>", "task": "<task>"}]}'
    )


def synthesis_system(lead: dict, context: str, web_rules: str) -> str:
    return (
        f"You are {lead['name']}, CEO of {_owner()}'s team. You asked your team to work on "
        f"{_owner()}'s request and their answers are below. Write the final answer: decide, combine "
        "and settle disagreements, credit people by name when it helps, and do not repeat everything "
        "they said. Under 250 words.\n" + _RULES + _context_block(context, web_rules)
    )


def _transcript(history: list[dict], message: str) -> str:
    lines = [f"{'User' if m['role'] == 'user' else 'You'}: {m['content'][:600]}" for m in history[-6:]]
    return ("Conversation so far:\n" + "\n".join(lines) + "\n\n" if lines else "") + f"Latest message: {message}"


# ---------- planning ----------

def parse_plan(text: str, team: list[dict]) -> tuple[str, list[dict]] | None:
    """The CEO's JSON plan, keeping only real team members (max MAX_ASSIGNMENTS)."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("assignments", []), list):
        return None
    keys = {a["key"]: a for a in team}
    by_name = {a["name"].lower(): a for a in team}
    out: list[dict] = []
    for item in data.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("agent") or "").strip()
        agent = keys.get(ref) or by_name.get(ref.lower())
        task = str(item.get("task") or "").strip()[:600]
        if agent and task and agent["key"] not in {o["agent"] for o in out}:
            out.append({"agent": agent["key"], "task": task})
        if len(out) >= MAX_ASSIGNMENTS:
            break
    return str(data.get("note") or "").strip()[:300], out


_WORD = re.compile(r"[a-záéíóúñü0-9]{4,}")


def heuristic_plan(message: str, team: list[dict]) -> tuple[str, list[dict]]:
    """Fallback when the model gives no usable plan: the two best skill matches."""
    words = set(_WORD.findall(message.lower()))

    def score(a: dict) -> int:
        text = f"{a['title']} {a['skills']} {a.get('description', '')}".lower()
        return sum(1 for w in words if w in text)

    ranked = sorted(team, key=lambda a: -score(a))
    picked = [a for a in ranked if score(a) > 0][:2] or ranked[:2]
    note = "Asking " + " and ".join(a["name"] for a in picked) + "." if picked else ""
    return note, [{"agent": a["key"], "task": message[:600]} for a in picked]


# ---------- running a turn ----------

class ChatTurn:
    """One user message to one agent. ``prepare`` resolves everything that can
    fail before the stream starts (unknown agent, provider unavailable), so the
    route can still answer with a normal HTTP error; ``events`` then runs it."""

    def __init__(self, agent_key: str, message: str, *, thread_id: int | None = None,
                 team: bool = True, provider: str | None = None) -> None:
        message = (message or "").strip()
        if not message:
            raise ValueError("message is required")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message is longer than {MAX_MESSAGE_CHARS} characters")
        info = roster()
        self.agents = info["agents"]
        self.agent = _find(self.agents, agent_key)
        if self.agent is None:
            raise LookupError(f"unknown agent {agent_key!r}")
        if thread_id is not None and _thread_agent(thread_id) != agent_key:
            raise LookupError("that conversation belongs to another agent")
        self.message = message
        self.thread_id = thread_id
        self.provider_name = provider or None
        self.mode = "team" if team and self.agent.get("lead") else "direct"
        self.provider = brain.resolve_provider(self.provider_name)   # may raise ProviderUnavailableError

    def _new_provider(self):
        return brain.resolve_provider(self.provider_name) if self.provider_name else brain.resolve_provider()

    def events(self) -> Iterator[dict]:
        if self.thread_id is None:
            self.thread_id = _new_thread(self.agent["key"], self.mode, self.message)
        history = _history(self.thread_id)
        _save(self.thread_id, "user", self.message)
        yield {"type": "start", "thread_id": self.thread_id, "agent": self.agent["key"], "mode": self.mode,
               "provider": self.provider.name, "model": getattr(self.provider, "model", None)}
        context, citations, web_rules = _context(self.message)
        yield {"type": "context", "citations": citations}
        if self.mode == "team":
            yield from self._team(history, context, web_rules)
        else:
            yield from self._direct(history, context, web_rules)
        yield {"type": "done", "thread_id": self.thread_id}

    def _stream_answer(self, system: str, messages: list[dict]) -> Iterator[dict]:
        parts: list[str] = []
        try:
            for chunk in self.provider.stream(system, messages):
                if chunk:
                    parts.append(chunk)
                    yield {"type": "delta", "content": chunk}
        except Exception as e:
            err = brain._translate_provider_error(e, self.provider.name)
            yield {"type": "error", "code": err.code, "message": err.message}
        if parts:
            _save(self.thread_id, "assistant", "".join(parts), self.provider.name)

    def _direct(self, history: list[dict], context: str, web_rules: str) -> Iterator[dict]:
        messages = history + [{"role": "user", "content": self.message}]
        if self.agent["kind"] == "brain":
            system = brain.SYSTEM_PROMPT_BASE.format(today=time.strftime("%Y-%m-%d"), preferences="(none yet)")
            system += "\n\nRelevant context (cite with [N]):\n" + (context or "(no matches)") + web_rules
        else:
            system = agent_system(self.agent, context, web_rules)
        yield from self._stream_answer(system, messages)

    def _plan(self, lead: dict, team: list[dict], history: list[dict]) -> tuple[str, list[dict]]:
        if self.provider.name != "mock":
            try:
                raw = brain.ask_with_deadline(
                    self._new_provider(), plan_system(lead, team),
                    [{"role": "user", "content": _transcript(history, self.message)}], PLAN_DEADLINE_SECONDS)
                parsed = parse_plan(raw, team)
                if parsed is not None:
                    return parsed
            except Exception:
                pass
        return heuristic_plan(self.message, team)

    def _team(self, history: list[dict], context: str, web_rules: str) -> Iterator[dict]:
        lead = self.agent
        team = team_of(lead, self.agents)
        note, assignments = self._plan(lead, team, history) if team else ("", [])
        cards = []
        for a in assignments:
            ag = _find(team, a["agent"])
            if ag:
                cards.append({"agent": ag["key"], "name": ag["name"], "title": ag["title"],
                              "avatar": ag["avatar"], "task": a["task"]})
        yield {"type": "plan", "note": note, "assignments": cards}
        if cards:
            _save(self.thread_id, "plan", json.dumps({"note": note, "assignments": cards}, ensure_ascii=False))
        outputs = yield from self._run_agents(lead, team, cards, context, web_rules)

        if outputs:
            answers = "\n\n".join(f"### {o['name']} ({o['title']}) — task: {o['task']}\n{o['text']}" for o in outputs)
            prompt = _transcript(history, self.message) + "\n\nYour team's answers:\n\n" + answers
            system = synthesis_system(lead, context, web_rules)
        else:
            prompt = _transcript(history, self.message)
            system = agent_system(lead, context, web_rules)
        yield {"type": "synthesis"}
        yield from self._stream_answer(system, [{"role": "user", "content": prompt}])

    def _run_agents(self, lead: dict, team: list[dict], cards: list[dict],
                    context: str, web_rules: str):
        """Run every assignment in its own thread and relay their chunks as they
        arrive. Returns the finished answers (generator return value)."""
        events: queue.Queue = queue.Queue()
        texts: dict[str, list[str]] = {c["agent"]: [] for c in cards}

        def work(card: dict) -> None:
            started = time.monotonic()
            agent = _find(team, card["agent"]) or {}
            try:
                provider = self._new_provider()
                system = agent_system(agent, context, web_rules, lead=lead)
                for chunk in provider.stream(system, [{"role": "user", "content": card["task"]}]):
                    if chunk:
                        events.put(("delta", card["agent"], chunk))
                events.put(("done", card["agent"], int((time.monotonic() - started) * 1000)))
            except Exception as e:
                err = brain._translate_provider_error(e, self.provider.name)
                events.put(("error", card["agent"], err.message))

        for card in cards:
            yield {"type": "agent_start", "agent": card["agent"]}
            threading.Thread(target=work, args=(card,), name=f"team-chat-{card['agent']}", daemon=True).start()

        pending = {c["agent"] for c in cards}
        deadline = time.monotonic() + AGENT_DEADLINE_SECONDS
        while pending:
            try:
                kind, key, value = events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                for key in sorted(pending):
                    yield {"type": "agent_error", "agent": key, "message": "timed out"}
                break
            if kind == "delta":
                texts[key].append(value)
                yield {"type": "agent_delta", "agent": key, "content": value}
            elif kind == "done":
                pending.discard(key)
                yield {"type": "agent_done", "agent": key, "ms": value}
            else:
                pending.discard(key)
                yield {"type": "agent_error", "agent": key, "message": value}

        outputs = []
        for card in cards:
            text = "".join(texts[card["agent"]]).strip()
            if text:
                outputs.append({**card, "text": text})
                _save(self.thread_id, f"agent:{card['agent']}", text, self.provider.name)
        return outputs
