"""Gap 4 step 1: draft a skill from a finished agent run, save only on review.

Deterministic template, redaction, write path is the folder importer.
No network. A skill is never saved by previewing.
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


def _finished_run(env, *, title="Summarise a PDF", prompt="Summarise the PDF at the given path.",
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
    assert db_tasks.finish_run(
        rid, status=status, output_text=output, tool_calls=tool_calls, error=error,
    )
    return rid, tid


def test_preview_builds_a_deterministic_draft_from_the_run(env):
    from ops_dashboard.skills import from_run

    rid, tid = _finished_run(env)
    first = from_run.preview(rid)
    second = from_run.preview(rid)
    assert first == second
    assert first["ok"] is True
    assert first["run_id"] == rid and first["task_id"] == tid
    assert first["name"] == "summarise-a-pdf"
    assert "Summarise the PDF" in first["description"]
    body = first["body"]
    assert "## Goal" in body and "Summarise the PDF at the given path." in body
    assert "## Steps" in body
    assert "Used `read_file` (ok)" in body
    assert "Used `write_note` (ok)" in body
    assert "## Inputs" in body
    assert "## Notes" in body
    assert f"run #{rid}" in body
    assert "The PDF is a short memo." in body
    assert first["exists"] is False
    assert list(env["vault"].iterdir()) == []


def test_preview_does_not_write_to_the_vault(env):
    from ops_dashboard.skills import from_run

    rid, _ = _finished_run(env)
    from_run.preview(rid)
    from_run.preview(rid)
    assert list(env["vault"].iterdir()) == []


def test_redacts_secrets_and_home_paths(env):
    from ops_dashboard.skills import from_run

    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123"
    home_file = env["home"] / "secret-notes" / "doc.txt"
    prompt = f"Read {home_file} with token {secret} and summarise it."
    rid, _ = _finished_run(
        env,
        title="Read private notes",
        prompt=prompt,
        output=f"Stored a copy at {home_file} token={secret}",
        tool_calls=[{"name": "read_file", "args": {"path": str(home_file), "token": secret}, "ok": True}],
    )
    draft = from_run.preview(rid)
    blob = draft["description"] + "\n" + draft["body"]
    assert secret not in blob
    assert str(env["home"]) not in blob
    assert str(home_file) not in blob
    assert "[REDACTED:" in blob
    assert "~" in blob
    assert "read_file" in draft["body"]


def test_running_and_missing_runs_are_rejected(env):
    from ops_dashboard import db_tasks
    from ops_dashboard.skills import from_run

    tid = db_tasks.create_task(None, "Still going", "do it", provider="mock", model="mock-1")
    live = db_tasks.start_run(tid, "mock", "mock-1")
    with pytest.raises(SkillError) as ei:
        from_run.preview(live)
    assert ei.value.code == "run_not_finished" and ei.value.status == 409

    with pytest.raises(SkillError) as ei:
        from_run.preview(9_999_999)
    assert ei.value.code == "run_not_found" and ei.value.status == 404

    with pytest.raises(SkillError) as ei:
        from_run.preview("nope")
    assert ei.value.code == "bad_run_id"


def test_save_requires_an_explicit_call_and_uses_edited_fields(env):
    from ops_dashboard.indexers import skills as idx
    from ops_dashboard.skills import catalog, from_run

    rid, _ = _finished_run(env)
    from_run.preview(rid)
    assert list(env["vault"].iterdir()) == []

    result = from_run.save(
        rid,
        "pdf-memo",
        "Turn a PDF into a short memo. Use when you have a PDF to summarise.",
        "# PDF memo\n\nDo it carefully.\n",
    )
    assert result["ok"] is True and result["name"] == "pdf-memo"
    dest = env["vault"] / "pdf-memo"
    assert dest.is_dir()
    text = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "name: pdf-memo" in text
    assert "Turn a PDF into a short memo" in text
    assert "Do it carefully." in text
    assert "Summarise a PDF" not in text  # the unedited title did not sneak in
    counters = idx.run()
    assert counters["indexed"] >= 1
    assert "pdf-memo" in [s["key"] for s in catalog.list_indexed()]


def test_edited_fields_are_validated(env):
    from ops_dashboard.skills import from_run

    rid, _ = _finished_run(env)
    with pytest.raises(SkillError) as ei:
        from_run.save(rid, "PDF Tools", "A fine description.", "# Body\n")
    assert ei.value.code == "name_format"
    assert list(env["vault"].iterdir()) == []

    with pytest.raises(SkillError) as ei:
        from_run.save(rid, "pdf-tools", "   ", "# Body\n")
    assert ei.value.code == "description_missing"

    with pytest.raises(SkillError) as ei:
        from_run.save(rid, "pdf-tools", "ok", "   ")
    assert ei.value.code == "empty_body"

    with pytest.raises(SkillError) as ei:
        from_run.save(rid, "pdf-tools", "x" * 1025, "# Body\n")
    assert ei.value.code == "description_too_long"
    assert list(env["vault"].iterdir()) == []


def test_existing_name_is_409_and_left_untouched(env):
    from ops_dashboard.skills import from_run
    from ops_dashboard.skills.import_folder import import_from_folder

    existing = env["tmp"] / "pdf-memo"
    existing.mkdir()
    (existing / "SKILL.md").write_text(
        "---\nname: pdf-memo\ndescription: Already in the vault.\n---\n\n# Keep me\n",
        encoding="utf-8",
    )
    import_from_folder(existing)
    original = (env["vault"] / "pdf-memo" / "SKILL.md").read_text(encoding="utf-8")

    rid, _ = _finished_run(env, title="PDF memo")
    draft = from_run.preview(rid)
    assert draft["name"] == "pdf-memo" and draft["exists"] is True

    with pytest.raises(SkillError) as ei:
        from_run.save(rid, "pdf-memo", "A new description for a colliding name.", "# New body\n")
    assert ei.value.code == "exists" and ei.value.status == 409
    assert (env["vault"] / "pdf-memo" / "SKILL.md").read_text(encoding="utf-8") == original


def test_error_run_can_be_drafted(env):
    from ops_dashboard.skills import from_run

    rid, _ = _finished_run(
        env, title="Almost", status="error", output=None, error="the tool timed out",
        tool_calls=[{"name": "browse", "args": {"url": "example.test"}, "ok": False}],
    )
    draft = from_run.preview(rid)
    assert "Used `browse` (failed)" in draft["body"]
    assert "the tool timed out" in draft["body"]


def test_source_does_not_talk_to_the_network():
    src = Path(__file__).resolve().parent.parent / "ops_dashboard" / "skills" / "from_run.py"
    text = src.read_text(encoding="utf-8")
    for needle in ("httpx", "urllib.request", "requests", "http://", "https://", "socket", "urlopen"):
        assert needle not in text, needle


def test_routes_are_local_only_and_preview_does_not_write(env):
    from http.server import ThreadingHTTPServer

    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            base + path, data=data, method="POST" if data else "GET",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    rid, _ = _finished_run(env)
    try:
        st, d = req("/api/learning/skills/from-run", {"run_id": rid})
        assert st == 200 and d["ok"] is True and d["name"] == "summarise-a-pdf"
        assert d["body"]
        assert list(env["vault"].iterdir()) == []

        st, d = req("/api/learning/skills/from-run/save", {
            "run_id": rid,
            "name": d["name"],
            "description": d["description"],
            "body": d["body"],
        })
        assert st == 200 and d["ok"] is True and d["key"] == "summarise-a-pdf"
        assert (env["vault"] / "summarise-a-pdf" / "SKILL.md").is_file()

        st, d = req("/api/learning/skills/from-run/save", {
            "run_id": rid,
            "name": "summarise-a-pdf",
            "description": "A second try at the same name.",
            "body": "# Nope\n",
        })
        assert st == 409 and d["error"] == "exists"

        st, _ = req("/api/learning/skills/from-run", {"run_id": rid},
                    headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
        st, _ = req("/api/learning/skills/from-run/save", {
            "run_id": rid, "name": "other", "description": "x", "body": "# y\n",
        }, headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_wires_the_save_as_skill_entry():
    src = (
        Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "app.js"
    ).read_text(encoding="utf-8")
    start = src.index("async function renderTaskDetail")
    detail = src[start:src.index("\ninitCanvasInteractions", start)]
    # The words of the panel are in the dictionary, so the page says them in
    # the language the person chose. What is checked here is the wiring.
    assert "skills.save_as_skill" in detail
    assert "skills.review_hint" in detail
    assert 'data-save-skill="' in detail or "data-save-skill=\"' +" in detail or "data-save-skill=" in detail
    assert "/api/learning/skills/from-run" in detail
    assert "/api/learning/skills/from-run/save" in detail
    assert "skill-from-run-save" in detail
    assert "skill-from-run-discard" in detail
    # Preview and save are separate calls; save is only from the Save button.
    assert detail.index("/api/learning/skills/from-run'") < detail.index("/api/learning/skills/from-run/save")
    assert "alert(" not in detail
    assert "confirm(" not in detail
    assert "prompt(" not in detail


def test_the_words_of_the_skill_panel_are_in_both_languages():
    """The panel is the one piece of the app frame that arrived after the
    pages were translated: its words have to exist in the two dictionaries
    or the person sees a key instead of a sentence."""
    import json

    web = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "i18n"
    en = json.loads((web / "en.json").read_text(encoding="utf-8"))
    es = json.loads((web / "es.json").read_text(encoding="utf-8"))
    assert set(en["skills"]) == set(es["skills"])
    for key in ("save_as_skill", "review_hint", "name", "description", "body",
                "save", "discard", "saved_as"):
        assert en["skills"][key] and es["skills"][key], key
    # The page asks for exactly those words.
    src = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "app.js").read_text(
        encoding="utf-8")
    for key in en["skills"]:
        assert f"skills.{key}" in src or key == "skill_word", key
