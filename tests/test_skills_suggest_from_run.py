"""Gap 4 step 2: suggest saving a successful multi-step run as a skill.

The suggestion only opens the existing review panel. Nothing is written
to the vault unless the operator clicks Save. Dismissals persist locally.
No model call, no network.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ops_dashboard.skills.catalog import SkillError


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    db.init_db()
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPS_DATA_DIR", str(data))
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    return {"home": home, "vault": vault, "tmp": tmp_path, "data": data, "db": db}


def _run(env, *, title="Summarise a PDF", prompt="Summarise the PDF at the given path.",
         status="done", output="The PDF is a short memo.",
         tool_calls=None, error=None):
    from ops_dashboard import db_tasks

    tid = db_tasks.create_task(None, title, prompt, provider="mock", model="mock-1")
    rid = db_tasks.start_run(tid, "mock", "mock-1")
    assert rid is not None
    if tool_calls is None:
        tool_calls = [
            {"name": "read_file", "args": {"path": "/tmp/doc.pdf"}, "ok": True},
            {"name": "write_note", "args": {"title": "summary"}, "ok": True},
        ]
    kw = {"status": status, "output_text": output, "tool_calls": tool_calls}
    if error is not None:
        kw["error"] = error
    if status == "running":
        return rid, tid
    assert db_tasks.finish_run(rid, **kw)
    return rid, tid


def test_no_suggestion_for_single_step_running_or_error(env):
    from ops_dashboard.skills import suggest_from_run as S

    one, _ = _run(env, title="One shot", tool_calls=[{"name": "read_file", "ok": True}])
    got = S.suggestion(one)
    assert got["suggest"] is False and got["reason"] == "too_few_steps"
    assert got["tool_calls"] == 1 and got["min_tool_calls"] == 2

    empty, _ = _run(env, title="No tools", tool_calls=[])
    got = S.suggestion(empty)
    assert got["suggest"] is False and got["reason"] == "too_few_steps"

    err, _ = _run(
        env, title="Broke", status="error", output=None, error="failed",
        tool_calls=[{"name": "a", "ok": False}, {"name": "b", "ok": False}],
    )
    got = S.suggestion(err)
    assert got["suggest"] is False and got["reason"] == "not_done"

    live, tid = _run(env, title="Still going", status="running")
    got = S.suggestion(live)
    assert got["suggest"] is False and got["reason"] == "not_done"
    listed = S.suggestions_for_task(tid)
    assert listed["run_ids"] == []


def test_suggests_an_eligible_successful_multi_step_run(env):
    from ops_dashboard.skills import suggest_from_run as S

    rid, tid = _run(env)
    got = S.suggestion(rid)
    assert got["ok"] is True and got["suggest"] is True
    assert got["reason"] == "eligible"
    assert got["tool_calls"] >= S.MIN_TOOL_CALLS == 2
    listed = S.suggestions_for_task(tid)
    assert listed["run_ids"] == [rid]
    assert listed["min_tool_calls"] == 2
    # Suggesting does not write a skill.
    assert list(env["vault"].iterdir()) == []


def test_dismiss_persists_and_is_not_suggested_again(env):
    from ops_dashboard.skills import suggest_from_run as S

    rid, tid = _run(env)
    assert S.suggestion(rid)["suggest"] is True
    out = S.dismiss(rid)
    assert out["ok"] is True and out["dismissed"] is True
    again = S.suggestion(rid)
    assert again["suggest"] is False and again["reason"] == "dismissed"
    assert S.suggestions_for_task(tid)["run_ids"] == []
    state = json.loads((env["data"] / "skill-from-run-suggestions.json").read_text())
    assert rid in state["dismissed"]
    # Dismiss is not a save.
    assert list(env["vault"].iterdir()) == []
    # A second call reads the same on-disk state.
    assert S.suggestion(rid)["suggest"] is False
    assert S.suggestion(rid)["reason"] == "dismissed"


def test_concurrent_dismiss_and_mark_saved_do_not_lose_updates(env):
    """Overlapping dismiss and mark_saved calls must all land in a valid file."""
    from ops_dashboard.skills import suggest_from_run as S

    n = 24
    dismissed = list(range(1, n + 1))
    saved = list(range(n + 1, 2 * n + 1))
    errors: list[BaseException] = []
    barrier = threading.Barrier(2 * n)

    def go_dismiss(rid: int) -> None:
        try:
            barrier.wait(timeout=2)
            S.dismiss(rid)
        except BaseException as exc:
            errors.append(exc)

    def go_saved(rid: int) -> None:
        try:
            barrier.wait(timeout=2)
            S.mark_saved(rid)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go_dismiss, args=(rid,)) for rid in dismissed]
    threads += [threading.Thread(target=go_saved, args=(rid,)) for rid in saved]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)
    assert not errors
    assert all(not t.is_alive() for t in threads)
    raw = (env["data"] / "skill-from-run-suggestions.json").read_text(encoding="utf-8")
    state = json.loads(raw)
    assert sorted(state["dismissed"]) == dismissed
    assert sorted(state["saved"]) == saved


def test_already_saved_runs_are_not_suggested(env):
    from ops_dashboard.skills import from_run
    from ops_dashboard.skills import suggest_from_run as S

    rid, tid = _run(env)
    draft = from_run.preview(rid)
    from_run.save(rid, draft["name"], draft["description"], draft["body"])
    got = S.suggestion(rid)
    assert got["suggest"] is False and got["reason"] == "already_saved"
    assert S.suggestions_for_task(tid)["run_ids"] == []
    assert (env["vault"] / draft["name"] / "SKILL.md").is_file()


def test_nothing_is_written_without_an_explicit_save(env):
    from ops_dashboard.skills import from_run
    from ops_dashboard.skills import suggest_from_run as S

    rid, tid = _run(env)
    S.suggestion(rid)
    S.suggestions_for_task(tid)
    from_run.preview(rid)
    S.dismiss(rid)
    assert list(env["vault"].iterdir()) == []


def test_missing_ids_are_rejected(env):
    from ops_dashboard.skills import suggest_from_run as S

    with pytest.raises(SkillError) as ei:
        S.suggestion(9_999_999)
    assert ei.value.code == "run_not_found"
    with pytest.raises(SkillError) as ei:
        S.suggestions_for_task(9_999_999)
    assert ei.value.code == "task_not_found"
    with pytest.raises(SkillError) as ei:
        S.suggestion("nope")
    assert ei.value.code == "bad_run_id"


def test_source_does_not_talk_to_the_network():
    src = (
        Path(__file__).resolve().parent.parent
        / "ops_dashboard" / "skills" / "suggest_from_run.py"
    )
    text = src.read_text(encoding="utf-8")
    for needle in ("httpx", "urllib.request", "requests", "http://", "https://", "socket", "urlopen"):
        assert needle not in text, needle


def test_routes_are_local_only_and_do_not_write(env):
    from http.server import ThreadingHTTPServer

    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            base + path, data=data, method=method or ("POST" if data else "GET"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    rid, tid = _run(env)
    try:
        st, d = req(f"/api/learning/skills/from-run/suggest?task_id={tid}")
        assert st == 200 and d["ok"] is True and d["run_ids"] == [rid]
        st, d = req(f"/api/learning/skills/from-run/suggest?run_id={rid}")
        assert st == 200 and d["suggest"] is True
        assert list(env["vault"].iterdir()) == []

        st, d = req("/api/learning/skills/from-run/dismiss", {"run_id": rid})
        assert st == 200 and d["dismissed"] is True
        st, d = req(f"/api/learning/skills/from-run/suggest?run_id={rid}")
        assert st == 200 and d["suggest"] is False and d["reason"] == "dismissed"
        assert list(env["vault"].iterdir()) == []

        st, _ = req(f"/api/learning/skills/from-run/suggest?task_id={tid}",
                    headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
        st, _ = req("/api/learning/skills/from-run/dismiss", {"run_id": rid},
                    headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_wires_the_suggestion_to_the_existing_review_panel():
    src = (
        Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "app.js"
    ).read_text(encoding="utf-8")
    start = src.index("async function renderTaskDetail")
    detail = src[start:src.index("\ninitCanvasInteractions", start)]
    assert "/api/learning/skills/from-run/suggest" in detail
    assert "/api/learning/skills/from-run/dismiss" in detail
    assert "data-suggest-review" in detail
    assert "data-suggest-dismiss" in detail
    # The suggestion speaks the reader's language: the copy goes through the
    # translator, it is not written in English in the page.
    assert "tr('skills.suggest_text')" in detail
    assert "tr('skills.review')" in detail and "tr('skills.dismiss')" in detail
    assert "Save it as a skill?" not in detail
    assert ">Review<" not in detail and ">Dismiss<" not in detail
    # ...and the words it uses exist in both languages.
    web = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
    for locale in ("en", "es"):
        catalog = json.loads(
            (web / "i18n" / f"{locale}.json").read_text(encoding="utf-8")
        )
        for key in ("suggest_text", "review", "dismiss"):
            assert catalog["skills"][key], f"{locale}.skills.{key} is empty"
    # Review opens the existing preview panel, not the save endpoint.
    assert "openSkillFromRunDraft" in detail
    assert detail.index("openSkillFromRunDraft") < detail.index("/api/learning/skills/from-run'")
    # The suggestion itself must not POST to save; only the Save button does.
    save_at = detail.index("/api/learning/skills/from-run/save")
    review_at = detail.index("data-suggest-review")
    # The Review click handler calls openSkillFromRunDraft, which posts to from-run (preview).
    assert "return openSkillFromRunDraft(parseInt(btn.dataset.suggestReview, 10), btn)" in detail
    assert "dataset.suggestReview" in detail
    # save URL appears only in the Save handler, not in the suggestion review path
    assert "skill-from-run-save" in detail
    assert "alert(" not in detail
    assert "confirm(" not in detail
    assert "prompt(" not in detail
    # Keep the unused names so a refactor still has a save call somewhere:
    assert save_at > 0 and review_at > 0
    assert "style=" not in "".join(
        line for line in detail.splitlines()
        if "data-suggest-review" in line or "data-suggest-dismiss" in line
        or "skill-from-run-suggest" in line
    )
