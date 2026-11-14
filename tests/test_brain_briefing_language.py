"""The panel's briefing speaks the language the person chose.

The panel's "Briefing" view shows this text as it is: it is not a set of labels
the page translates, it is words the server wrote. So the server has to write
them in the language the person chose — and the numbers in it have to be the
ones the workspace actually holds, not the ones somebody wrote down once.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_request_language(None)
    return db, tmp_path


def _projects(monkeypatch, n: int) -> None:
    """The workspace holds n companies, as the dashboard would see them."""
    from ops_dashboard import brain

    rows = [
        {
            "name": f"p{i}",
            "key": f"p{i}",
            "all_urls_ok": True,
            "last_activity": 0,
            "repos": [],
            "mention_count": 0,
        }
        for i in range(n)
    ]
    monkeypatch.setattr(brain.projects, "all_projects_summary", lambda: rows)


def _url(env, name: str, ok: int, when: int | None = None) -> None:
    """One check of one URL. The name is unique: a check replaces the previous
    one, so there is no history of checks to read."""
    from ops_dashboard import db

    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO live_urls (name, url, checked_at, status_code, ok) VALUES (?,?,?,?,?)",
            (name, f"https://{name}.test", int(time.time()) if when is None else when,
             200 if ok else 500, ok),
        )


def _urls(env, ok_names: list[str], down_names: list[str], never: list[str] | None = None) -> None:
    """URLs that answer, URLs that are down, and URLs nobody has checked yet."""
    from ops_dashboard.indexers import live_urls

    live_urls.ensure_schema()
    for n in ok_names:
        _url(env, n, 1)
    for n in down_names:
        _url(env, n, 0)
    for n in never or []:
        _url(env, n, 0, when=0)


def test_the_panel_briefing_speaks_the_language_the_person_chose(env, monkeypatch):
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 2)
    _urls(env, ["a", "b", "c"], [])
    i18n.set_locale("es")
    text = brain.briefing()

    assert "Resumen" in text
    assert "Tus empresas (2)" in text
    assert "URLs en vivo" in text
    assert "TODOs abiertos" in text
    assert "Documentos indexados" in text
    assert "todo en pie" in text
    # Nothing of the English version left behind.
    for leftover in ("Briefing", "Your companies", "Live URLs", "Open TODOs",
                     "Indexed documents", "all up", "last activity", "mentions"):
        assert leftover not in text, leftover


def test_the_panel_briefing_speaks_english_when_that_is_the_choice(env, monkeypatch):
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 2)
    _urls(env, ["a", "b", "c"], [])
    i18n.set_locale("en")
    text = brain.briefing()

    assert "Briefing" in text
    assert "Your companies (2)" in text
    assert "Live URLs" in text
    assert "Open TODOs" in text
    assert "Indexed documents" in text
    assert "all up" in text
    for leftover in ("Resumen", "Tus empresas", "URLs en vivo", "TODOs abiertos",
                     "Documentos indexados", "todo en pie"):
        assert leftover not in text, leftover


def test_the_numbers_in_the_briefing_are_counted_not_written_down(env, monkeypatch):
    """The old text said "your 12 companies" and "all 13 URLs" whatever the
    workspace held. The text lied as soon as the workspace changed."""
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 3)
    _urls(env, ["a", "b", "c", "d", "e"], [])
    i18n.set_locale("es")
    text = brain.briefing()

    assert "Tus empresas (3)" in text
    assert "URLs operativas: 5" in text
    assert "12 empresas" not in text
    assert "13 URLs" not in text


def test_the_briefing_counts_the_urls_that_are_down(env, monkeypatch):
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 1)
    _urls(env, ["a", "b"], ["broken"])
    i18n.set_locale("es")
    text = brain.briefing()

    assert "URLs caídas: 1" in text
    assert "broken" in text
    assert "URLs operativas" not in text


def test_a_url_nobody_checked_is_not_counted_as_down(env, monkeypatch):
    """A URL that was configured but never checked is not a URL that is down."""
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 1)
    _urls(env, ["a"], [], never=["never"])
    i18n.set_locale("es")
    text = brain.briefing()

    assert "URLs operativas: 1" in text
    assert "never" not in text


def test_a_workspace_with_no_urls_says_so_instead_of_pretending(env, monkeypatch):
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 1)
    i18n.set_locale("es")
    text = brain.briefing()

    assert "Todavía no se vigila ninguna URL" in text
    assert "0 URLs" not in text


def test_the_words_of_the_person_are_not_translated(env, monkeypatch):
    """The briefing says what happened; what happened is in the person's words."""
    from ops_dashboard import brain, i18n

    _projects(monkeypatch, 1)
    i18n.set_locale("es")
    monkeypatch.setattr(
        brain.projects, "all_projects_summary",
        lambda: [{
            "name": "Ship it", "key": "acme", "all_urls_ok": False,
            "last_activity": 0, "repos": [], "mention_count": 0,
        }],
    )
    text = brain.briefing()

    assert "Ship it" in text
    assert "alguna caída" in text


def test_the_model_is_told_which_language_to_write_in(env, monkeypatch):
    """The summary is the model's, but the language is ours to ask for."""
    from ops_dashboard import brain, i18n

    seen: dict[str, str] = {}

    class FakeProvider:
        name = "anthropic"
        model = "fake"

        def ask(self, system, messages):
            seen["system"] = system
            seen["digest"] = messages[0]["content"]
            return "Resumen del modelo."

    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: FakeProvider())

    _projects(monkeypatch, 1)
    i18n.set_locale("es")
    text = brain.briefing()

    assert "Español" in seen["system"]
    assert "Resumen del modelo." in text
    assert "(datos crudos)" in text

    i18n.set_locale("en")
    brain.briefing()
    assert "English" in seen["system"]


def test_the_counts_are_written_in_the_singular_when_there_is_one(env, monkeypatch):
    """One repo is one repo, not "1 repos"."""
    from ops_dashboard import brain, i18n

    monkeypatch.setattr(
        brain.projects, "all_projects_summary",
        lambda: [{
            "name": "Acme", "key": "acme", "all_urls_ok": True,
            "last_activity": 0, "repos": ["/tmp/acme"], "mention_count": 1,
        }],
    )
    i18n.set_locale("es")
    text = brain.briefing()
    assert "1 repo" in text
    assert "1 mención" in text
    assert "1 repos" not in text
    assert "1 menciones" not in text


def test_both_languages_carry_the_same_words(env):
    from ops_dashboard import brain

    assert set(brain._BRIEFING_WORDS["en"]) == set(brain._BRIEFING_WORDS["es"])


def test_the_command_line_heading_speaks_the_language_too(env):
    from ops_dashboard import brain, i18n

    i18n.set_locale("es")
    assert brain.briefing_title() == "Resumen de la mañana"
    i18n.set_locale("en")
    assert brain.briefing_title() == "Morning briefing"
