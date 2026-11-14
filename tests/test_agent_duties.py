"""The shift of each agent: the brief it works on when nobody asks.

The C-Suite had a face (the marks of the identity kit) and a playbook (the
skill packs, each skill saying when to use it), and nothing else: the
triggers that would wake an agent on its own existed and had never been used
once, and an agent that woke up woke up as a generic assistant with a role
string in the prompt.

Three things have to be true for "cada agente tiene su turno":

* **The brief exists.** Every role of the C-Suite has a standing brief, and
  it is written from that role's own skills — not from a list of duties kept
  somewhere else that can drift away from the playbook.
* **The shift is opt-in.** Putting an agent on shift is the person's
  decision; turning it off keeps the record of what it did. A trigger a
  person created by hand is not a shift.
* **The wake-up carries the pack.** The agent wakes up as itself, with its
  own criteria.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from ops_dashboard import db, db_agents, duty, migrations
from ops_dashboard.skills import packs

WEB = Path(__file__).resolve().parents[1] / "ops_dashboard" / "web"


# ============================================================
# The brief
# ============================================================

def test_every_pack_of_the_c_suite_has_a_standing_brief():
    found = packs.discover_packs()
    assert found, "no packs were found"
    without = [p["id"] for p in found if not p.get("duty")]
    assert without == [], f"packs without a standing brief: {without}"


def test_the_brief_of_every_role_says_when_it_wakes_up_and_what_it_does():
    duties = duty.duties()
    assert len(duties) >= 12, f"only {len(duties)} roles have a brief"
    for d in duties:
        assert d["role"], d
        assert len(d["cron"].split()) == 5, f"{d['role']}: {d['cron']} is not a 5-field cron"
        assert re.match(r"^[\d*/,\- ]+$", d["cron"]), f"{d['role']}: {d['cron']}"
        assert d["prompt"].strip(), f"{d['role']} has no brief"


def test_the_brief_is_written_from_the_skills_of_the_role():
    """The brief comes from the playbook: a role's own skills say when they
    apply, and the brief uses them. A brief written somewhere else would
    drift away from the pack the day the pack changes."""
    for d in duty.duties():
        named = [s for s in d["skills"] if s and s in d["prompt"]]
        assert named, f"the brief of {d['role']} does not mention any of its own skills"


def test_no_two_agents_wake_at_the_same_minute():
    minutes = [d["cron"].split()[0] for d in duty.duties()]
    assert len(minutes) == len(set(minutes)), f"two roles share a minute: {minutes}"


def _fires_at(spec: str, hour: str, minute: str) -> bool:
    """Whether a cron fires at that hour and minute.

    Only what the crons of this app use: ``*``, ``N``, ``N-M`` and ``N,M``.
    """
    m_field, h_field = spec.split()[:2]

    def ok(field: str, value: str) -> bool:
        if field == "*":
            return True
        for part in field.split(","):
            if part == value:
                return True
            if "-" in part:
                lo, hi = part.split("-")
                if lo.isdigit() and hi.isdigit() and int(lo) <= int(value) <= int(hi):
                    return True
        return False

    return ok(m_field, minute) and ok(h_field, hour)


def test_a_shift_never_lands_on_the_minute_of_a_process_of_the_app():
    """The scheduler ticks on one thread: an agent that wakes up at the same
    minute as the backup or the briefing makes the other one late."""
    from ops_dashboard import scheduler

    jobs = scheduler.system_job_specs()
    assert jobs, "the app has no system jobs"
    for d in duty.duties():
        minute, hour = d["cron"].split()[:2]
        for name, spec in jobs:
            assert not _fires_at(spec, hour, minute), \
                f"{d['role']} would wake up at the same time as {name} ({spec})"


def test_the_brief_of_a_role_is_found_by_its_name(env):
    d = duty.duty_for("cto")
    assert d and d["role"] == "cto"
    assert duty.duty_for("CTO")["role"] == "cto"
    assert duty.duty_for("") is None
    assert duty.duty_for("copywriter") is None


# ============================================================
# The shift
# ============================================================

def test_an_agent_is_not_on_shift_until_the_person_says_so(env):
    s = duty.shift_of(env["cto"])
    assert s["enabled"] is False
    assert s["has_duty"] is True
    assert s["cron"] == duty.duty_for("cto")["cron"]
    assert db_agents.list_triggers(env["cto"], enabled_only=False) == []


def test_putting_an_agent_on_shift_creates_its_own_trigger(env):
    s = duty.set_shift(env["cto"], True)
    assert s["enabled"] is True
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 1
    t = triggers[0]
    assert t["source"] == "shift"
    assert t["kind"] == "cron"
    assert t["spec"] == duty.duty_for("cto")["cron"]
    assert t["prompt"] == duty.duty_for("cto")["prompt"]
    assert t["enabled"] == 1


def test_taking_an_agent_off_shift_keeps_the_record_of_what_it_did(env):
    duty.set_shift(env["cto"], True)
    tid = db_agents.list_triggers(env["cto"], enabled_only=False)[0]["id"]
    db_agents.record_trigger_fire(tid, status="ok")
    s = duty.set_shift(env["cto"], False)
    assert s["enabled"] is False
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 1, "the trigger was deleted instead of disabled"
    assert triggers[0]["enabled"] == 0
    assert triggers[0]["last_status"] == "ok"
    assert triggers[0]["last_fired_at"]


def test_going_back_on_shift_does_not_pile_up_triggers(env):
    duty.set_shift(env["cto"], True)
    duty.set_shift(env["cto"], False)
    duty.set_shift(env["cto"], True)
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 1
    assert triggers[0]["enabled"] == 1


def test_a_trigger_a_person_created_is_not_a_shift(env):
    db_agents.add_trigger(env["cto"], "cron", "0 7 * * *", prompt="hello")
    s = duty.shift_of(env["cto"])
    assert s["enabled"] is False, "a manual trigger was taken for a shift"
    duty.set_shift(env["cto"], True)
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 2
    assert sorted(t["source"] for t in triggers) == ["manual", "shift"]


def test_the_shift_says_when_the_agent_last_worked_and_how_it_went(env):
    duty.set_shift(env["cto"], True)
    tid = db_agents.list_triggers(env["cto"], enabled_only=False)[0]["id"]
    db_agents.record_trigger_fire(tid, status="error", error="the provider said no")
    s = duty.shift_of(env["cto"])
    assert s["last_status"] == "error"
    assert s["last_error"] == "the provider said no"
    assert s["last_fired_at"]


def test_an_agent_whose_role_has_no_brief_cannot_go_on_shift(env):
    with pytest.raises(ValueError) as e:
        duty.set_shift(env["nobody"], True)
    assert "no_duty_for_role" in str(e.value)


def test_an_agent_that_does_not_exist_has_no_shift(env):
    with pytest.raises(ValueError) as e:
        duty.set_shift(9999, True)
    assert "no_agent" in str(e.value)


def test_a_shift_can_be_turned_off_after_the_role_changes(env):
    """The switch must never be locked by the thing it is there to stop.

    The role of an agent can change while it is on shift. The shift keeps
    firing the old brief until somebody stops it, so asking for a brief to
    turn it off would close the switch on the very thing that needs it.
    """
    from ops_dashboard import db_teams

    duty.set_shift(env["cto"], True)
    db_teams.update_member(env["cto"], role="copywriter")

    s = duty.set_shift(env["cto"], False)
    assert s["enabled"] is False
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 1
    assert triggers[0]["enabled"] == 0, "the shift kept running the old brief"


def test_a_brief_is_only_needed_to_turn_a_shift_on(env):
    from ops_dashboard import db_teams

    db_teams.update_member(env["cto"], role="copywriter")
    with pytest.raises(ValueError) as e:
        duty.set_shift(env["cto"], True)
    assert "no_duty_for_role" in str(e.value)


def test_the_panel_shows_an_agent_whose_role_changed(env):
    """Otherwise the person cannot even see the shift that is still
    running."""
    from ops_dashboard import db_teams, routes_duty

    duty.set_shift(env["cto"], True)
    db_teams.update_member(env["cto"], role="copywriter")

    h = FakeHandler()
    routes_duty._duties(h)
    _status, body = h.answer
    agents = {a["id"]: a for a in body["agents"]}
    assert env["cto"] in agents, "the agent with the stale shift is not in the panel"
    stale = agents[env["cto"]]
    assert stale["on_shift"] is True
    assert stale["has_duty"] is False
    assert stale["role"] == "copywriter"


# ============================================================
# The migration
# ============================================================

def test_a_trigger_that_existed_before_the_shift_is_a_manual_one():
    """An old database has no 'source' column. After the migration every
    trigger it had is 'manual', because nobody could have put an agent on
    shift: the feature did not exist yet."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE agent_triggers (id INTEGER PRIMARY KEY, agent_id INTEGER, "
        "kind TEXT, spec TEXT, prompt TEXT, enabled INTEGER DEFAULT 1, "
        "created_at INTEGER, updated_at INTEGER)")
    conn.execute("INSERT INTO agent_triggers (agent_id, kind, spec, enabled) "
                 "VALUES (1, 'cron', '0 9 * * *', 1)")
    conn.execute("PRAGMA user_version = 40")

    migrations.run_migrations(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_triggers)")}
    assert "source" in cols
    row = conn.execute("SELECT source FROM agent_triggers").fetchone()
    assert row["source"] == "manual"


# ============================================================
# The routes
# ============================================================

class FakeHandler:
    """The little a route needs: somewhere to send the answer, and a body."""

    def __init__(self, body: dict | None = None, *, local: bool = True):
        self.sent: list = []
        self._local = local
        payload = json.dumps(body if body is not None else {}).encode()
        self.headers = {"Content-Length": str(len(payload))}
        self.rfile = io.BytesIO(payload)

    def _is_local(self) -> bool:
        return self._local

    def _send(self, status, body, ctype, headers=None):
        self.sent.append((status, json.loads(body.decode())))

    @property
    def answer(self) -> tuple[int, dict]:
        return self.sent[-1]


def test_the_duties_endpoint_lists_the_roles_and_who_is_on_shift(env):
    from ops_dashboard import routes_duty

    h = FakeHandler()
    routes_duty._duties(h)
    status, body = h.answer
    assert status == 200
    assert body["ok"] is True
    roles = {d["role"] for d in body["duties"]}
    assert "cto" in roles
    agents = {a["role"]: a for a in body["agents"]}
    assert agents["cto"]["on_shift"] is False
    assert agents["cto"]["cron"]
    assert agents["cto"]["prompt"]

    # On shift: the panel has to say so, or the switch would be a lie.
    duty.set_shift(env["cto"], True)
    h2 = FakeHandler()
    routes_duty._duties(h2)
    _status2, body2 = h2.answer
    agents2 = {a["role"]: a for a in body2["agents"]}
    assert agents2["cto"]["on_shift"] is True


def test_putting_an_agent_on_shift_over_the_api(env):
    from ops_dashboard import routes_duty

    h = FakeHandler({"enabled": True})
    routes_duty._shift(h, agent_id=str(env["cto"]))
    status, body = h.answer
    assert status == 200
    assert body["ok"] is True
    assert body["shift"]["enabled"] is True
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 1
    assert triggers[0]["source"] == "shift"


def test_taking_an_agent_off_shift_over_the_api(env):
    from ops_dashboard import routes_duty

    duty.set_shift(env["cto"], True)
    h = FakeHandler({"enabled": False})
    routes_duty._shift(h, agent_id=str(env["cto"]))
    status, body = h.answer
    assert status == 200
    assert body["shift"]["enabled"] is False
    triggers = db_agents.list_triggers(env["cto"], enabled_only=False)
    assert len(triggers) == 1
    assert triggers[0]["enabled"] == 0


def test_an_agent_without_a_brief_is_not_found(env):
    from ops_dashboard import routes_duty

    h = FakeHandler({"enabled": True})
    routes_duty._shift(h, agent_id=str(env["nobody"]))
    status, body = h.answer
    assert status == 404
    assert body["error"] == "no_duty"


def test_a_broken_id_is_a_bad_request(env):
    from ops_dashboard import routes_duty

    h = FakeHandler({"enabled": True})
    routes_duty._shift(h, agent_id="not-a-number")
    status, body = h.answer
    assert status == 400
    assert body["error"] == "bad_agent"


def test_the_shift_is_local_only(env):
    """Putting an agent on shift makes it spend tokens on this Mac."""
    from ops_dashboard import routes_duty

    h = FakeHandler({"enabled": True}, local=False)
    routes_duty._shift(h, agent_id=str(env["cto"]))
    status, body = h.answer
    assert status == 403
    assert body["error"] == "local_only"
    assert db_agents.list_triggers(env["cto"], enabled_only=False) == []


def test_the_shift_route_is_registered_with_a_path_that_can_match():
    """A route written with braces is registered and never fires: the router
    reads <name>, not {name}. Registered is not the same as reachable."""
    from ops_dashboard import routes_meta  # noqa: F401  (registers the routes)
    from ops_dashboard.routing import router

    patterns = [raw for _m, raw in router.registered()]
    assert "/api/agents/<agent_id>/shift" in patterns
    assert "/api/agents/{agent_id}/shift" not in patterns


# ============================================================
# The wake-up
# ============================================================

def test_an_agent_wakes_up_with_its_own_pack(env, tmp_path, monkeypatch):
    """The agent must wake up as itself, with its own criteria — not as a
    generic assistant that happens to have a role string in the prompt."""
    from ops_dashboard import brain, scheduler

    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    skill = vault / "code-review-rigor"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: code-review-rigor\ndescription: Review code carefully\n---\n\nBody.\n",
        encoding="utf-8")
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_skills (agent_id, skill_key, enabled, created_at) "
            "VALUES (?, 'code-review-rigor', 1, ?)", (env["cto"], int(time.time())))

    seen: dict = {}

    class Prov:
        name = "fake"

        def available(self):
            return True

    def fake_ask(prov, system_prompt, messages):
        seen["system"] = system_prompt
        return ("the answer",)

    monkeypatch.setattr(brain, "resolve_provider", lambda name=None: Prov())
    monkeypatch.setattr(brain, "ask_with_connector_tools", fake_ask)

    result = scheduler._wake_agent(env["cto"], "review the week")

    assert result.get("ok") is True, result
    assert "## Your skills" in seen["system"]
    assert "code-review-rigor" in seen["system"]


def test_a_broken_pack_does_not_stop_the_agent_from_waking_up(env, monkeypatch):
    """A skill that cannot be read must not take the agent down with it: the
    run is worth more than the pack."""
    from ops_dashboard import brain, scheduler

    def boom(*_a, **_k):
        raise RuntimeError("the vault is on fire")

    monkeypatch.setattr(packs, "agent_skills_block", boom)

    class Prov:
        name = "fake"

        def available(self):
            return True

    seen: dict = {}

    def fake_ask(prov, system_prompt, messages):
        seen["system"] = system_prompt
        return ("the answer",)

    monkeypatch.setattr(brain, "resolve_provider", lambda name=None: Prov())
    monkeypatch.setattr(brain, "ask_with_connector_tools", fake_ask)

    result = scheduler._wake_agent(env["cto"], "review the week")

    assert result.get("ok") is True, result
    assert "## Your skills" not in seen["system"]


# ============================================================
# The page
# ============================================================

def test_the_agents_page_shows_the_shifts():
    html = (WEB / "agents.html").read_text(encoding="utf-8")
    assert 'id="shifts"' in html
    assert "/api/agents/duties" in html
    assert "/shift" in html
    assert "shift_stale" in html


def test_both_languages_say_the_same_about_the_shifts():
    en = json.loads((WEB / "i18n" / "agents.en.json").read_text(encoding="utf-8"))["agents"]
    es = json.loads((WEB / "i18n" / "agents.es.json").read_text(encoding="utf-8"))["agents"]
    assert set(en) == set(es), f"only in en: {set(en) - set(es)}, only in es: {set(es) - set(en)}"
    for key in ("shifts", "shift_on", "shift_off", "shift_on_toast", "shift_off_toast",
                "shift_last", "shift_stale", "no_duty", "never"):
        assert en.get(key), f"the key {key} is missing in english"
        assert es.get(key), f"the key {key} is missing in spanish"


# ============================================================
# The fixture
# ============================================================

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A database with a team, an agent whose role has a brief, and an agent
    whose role has none."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "duty.db")
    db.init_db()
    now = int(time.time())
    with db.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        cur = conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
            "level, is_ceo, created_at, updated_at) "
            "VALUES (1, 'Atlas (CTO)', 'ai_agent', 'cto', 'active', '{}', 'CTO', 'ceo', 1, ?, ?)",
            (now, now))
        cto = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, notes, "
            "level, is_ceo, created_at, updated_at) "
            "VALUES (1, 'Nora (Copywriter)', 'ai_agent', 'copywriter', 'active', '{}', 'Copy', "
            "'agent', 0, ?, ?)",
            (now, now))
        nobody = int(cur.lastrowid)
    return {"cto": cto, "nobody": nobody}
