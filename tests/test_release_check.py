"""scripts/release_check.py: the dry-run pre-flight for a desktop release.

Every test builds a throwaway repository in ``tmp_path`` (files + ``git init``);
nothing here touches the real checkout, the network or any remote.
"""
from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_check.py"
_spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
sys.modules["release_check"] = rc  # dataclasses looks the module up here
_spec.loader.exec_module(rc)

VERSION = "1.2.1"
TODAY = dt.date(2026, 11, 3)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def fake_pubkey(keyid: str, comment_keyid: str | None = None) -> str:
    """Tauri-style value: base64 of a minisign .pub file with the given key id."""
    blob = b"Ed" + bytes.fromhex(keyid)[::-1] + bytes(32)
    text = f"untrusted comment: minisign public key: {comment_keyid or keyid}\n{base64.b64encode(blob).decode()}\n"
    return base64.b64encode(text.encode()).decode()


def git(path: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_repo(tmp_path: Path, version: str = VERSION) -> Path:
    """A minimal repository that passes every check."""
    r = tmp_path / "repo"
    (r / "ops_dashboard").mkdir(parents=True)
    (r / "desktop" / "src-tauri").mkdir(parents=True)
    (r / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (r / "ops_dashboard" / "__init__.py").write_text(f'"""x"""\n__version__ = "{version}"\n')
    (r / "desktop" / "package.json").write_text(json.dumps({"name": "x", "version": version}))
    (r / "desktop" / "src-tauri" / "Cargo.toml").write_text(
        f'[package]\nname = "{rc.CARGO_PACKAGE}"\nversion = "{version}"\n\n[dependencies]\nserde = "1"\n')
    (r / "desktop" / "src-tauri" / "Cargo.lock").write_text(
        f'version = 4\n\n[[package]]\nname = "serde"\nversion = "1.0.0"\n\n'
        f'[[package]]\nname = "{rc.CARGO_PACKAGE}"\nversion = "{version}"\n')
    (r / "desktop" / "src-tauri" / "tauri.conf.json").write_text(json.dumps({
        "version": version,
        "bundle": {"macOS": {"minimumSystemVersion": "13.0"}},
        "plugins": {"updater": {"endpoints": [rc.EXPECTED_ENDPOINT], "pubkey": fake_pubkey(rc.EXPECTED_KEY_ID)}},
    }))
    (r / "desktop" / "UPDATER_KEY_ID").write_text(rc.EXPECTED_KEY_ID + "\n")
    (r / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## Unreleased\n\n"
        f"## {version} — 2026-11-02 — Something useful\n\n- Fixed a thing.\n- Added another.\n\n"
        "## 1.2.0 — 2026-10-28 — Older\n\n- Old entry.\n")
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def results(root: Path, **kw):
    return rc.run_checks(root, kw.pop("version", VERSION), kw.pop("notes_file", None), TODAY)


def by(res, check: str):
    return [r for r in res if r.check == check]


def levels(res, check: str) -> set[str]:
    return {r.level for r in by(res, check)}


def edit_json(path: Path, fn) -> None:
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def commit_all(r: Path) -> None:
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "change")


# ── happy path ───────────────────────────────────────────────────────────────


def test_a_good_repo_passes_everything(tmp_path):
    r = make_repo(tmp_path)
    res = results(r)
    assert rc.exit_code(res) == 0
    assert {x.level for x in res} == {rc.OK}, [x for x in res if x.level != rc.OK]


def test_cli_exit_codes_and_output(tmp_path, capsys):
    r = make_repo(tmp_path)
    assert rc.main(["--version", VERSION, "--root", str(r)]) == 0
    out = capsys.readouterr().out
    assert not any(line.startswith("FAIL") for line in out.splitlines())
    assert "Ready to run" in out
    assert rc.main(["--version", "9.9.9", "--root", str(r)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "NOT ready" in out


def test_version_must_look_like_x_y_z(tmp_path, capsys):
    r = make_repo(tmp_path)
    assert rc.main(["--version", "v1.2.1", "--root", str(r)]) == 2
    assert rc.main(["--version", "1.2", "--root", str(r)]) == 2
    with pytest.raises(SystemExit) as exc:
        rc.main(["--root", str(r)])
    assert exc.value.code == 2


# ── 1. versions ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel,mutate", [
    ("pyproject.toml", lambda p: p.write_text(p.read_text().replace(VERSION, "1.2.0"))),
    ("ops_dashboard/__init__.py", lambda p: p.write_text(p.read_text().replace(VERSION, "1.2.0"))),
    ("desktop/src-tauri/Cargo.toml", lambda p: p.write_text(p.read_text().replace(VERSION, "1.2.0"))),
    ("desktop/src-tauri/tauri.conf.json", lambda p: edit_json(p, lambda d: d.update(version="1.2.0"))),
    ("desktop/package.json", lambda p: edit_json(p, lambda d: d.update(version="1.1.1"))),
])
def test_a_version_mismatch_fails(tmp_path, rel, mutate):
    r = make_repo(tmp_path)
    mutate(r / rel)
    commit_all(r)
    res = results(r)
    bad = [x for x in by(res, "version") if x.level == rc.FAIL]
    assert len(bad) == 1 and rel in bad[0].message
    assert rc.exit_code(res) == 1


def test_package_json_is_optional(tmp_path):
    r = make_repo(tmp_path)
    (r / "desktop" / "package.json").unlink()
    commit_all(r)
    assert rc.FAIL not in levels(results(r), "version")


def test_a_missing_required_file_fails(tmp_path):
    r = make_repo(tmp_path)
    (r / "pyproject.toml").unlink()
    commit_all(r)
    assert rc.FAIL in levels(results(r), "version")


def test_stale_lock_files_only_warn(tmp_path):
    r = make_repo(tmp_path)
    lock = r / "desktop" / "src-tauri" / "Cargo.lock"
    lock.write_text(lock.read_text().replace(VERSION, "1.2.0"))
    (r / "desktop" / "package-lock.json").write_text(json.dumps({"version": "1.0.0", "packages": {"": {"version": "1.0.0"}}}))
    commit_all(r)
    res = results(r)
    assert levels(res, "version") == {rc.OK, rc.WARN}
    assert rc.exit_code(res) == 0
    assert rc.exit_code(res, strict=True) == 1


def test_unreadable_version_file_is_reported_not_raised(tmp_path):
    r = make_repo(tmp_path)
    (r / "pyproject.toml").write_text("this is not toml = = =")
    commit_all(r)
    assert rc.FAIL in levels(results(r), "version")


# ── 2/3. changelog and notes ─────────────────────────────────────────────────


def test_changelog_without_the_version_fails(tmp_path):
    r = make_repo(tmp_path)
    res = results(r, version="1.2.2")
    assert rc.FAIL in levels(res, "changelog")
    assert rc.FAIL in levels(res, "notes")


def test_changelog_heading_needs_a_date(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(c.read_text().replace("— 2026-11-02 ", ""))
    commit_all(r)
    res = results(r)
    assert any(x.level == rc.FAIL and "date" in x.message for x in by(res, "changelog"))


@pytest.mark.parametrize("heading", [
    f"## [{VERSION}] - 2026-11-02",
    f"## v{VERSION} (2026-11-02)",
    f"## {VERSION} — 2026-11-02 — Title",
])
def test_accepted_heading_styles(tmp_path, heading):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(f"# Changelog\n\n{heading}\n\n- Something.\n")
    commit_all(r)
    res = results(r)
    assert levels(res, "changelog") == {rc.OK} and levels(res, "notes") == {rc.OK}


def test_version_in_heading_is_matched_exactly(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text("# Changelog\n\n## 1.2.10 — 2026-11-02\n\n- Not this one.\n\n## 11.2.1 — 2026-11-01\n\n- Nor this.\n")
    commit_all(r)
    assert rc.FAIL in levels(results(r), "changelog")


def test_an_invalid_calendar_date_does_not_count(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(f"# Changelog\n\n## {VERSION} — 2026-13-45\n\n- Something.\n")
    commit_all(r)
    assert rc.FAIL in levels(results(r), "changelog")


def test_a_future_date_warns(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(f"# Changelog\n\n## {VERSION} — 2027-01-01\n\n- Something.\n")
    commit_all(r)
    res = results(r)
    assert rc.WARN in levels(res, "changelog") and rc.FAIL not in levels(res, "changelog")


def test_empty_changelog_section_fails_the_notes_check(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(f"# Changelog\n\n## {VERSION} — 2026-11-02\n\n\n## 1.2.0 — 2026-10-28\n\n- Old.\n")
    commit_all(r)
    res = results(r)
    assert levels(res, "changelog") == {rc.OK}
    assert levels(res, "notes") == {rc.FAIL}


def test_notes_file_replaces_the_changelog_section(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(f"# Changelog\n\n## {VERSION} — 2026-11-02\n")
    commit_all(r)
    assert levels(results(r), "notes") == {rc.FAIL}
    notes = tmp_path / "notes.md"
    notes.write_text("Release notes.\n")
    assert levels(results(r, notes_file=notes), "notes") == {rc.OK}
    notes.write_text("  \n\n")
    assert levels(results(r, notes_file=notes), "notes") == {rc.FAIL}
    assert levels(results(r, notes_file=tmp_path / "missing.md"), "notes") == {rc.FAIL}


def test_entries_left_under_unreleased_warn(tmp_path):
    r = make_repo(tmp_path)
    c = r / "CHANGELOG.md"
    c.write_text(c.read_text().replace("## Unreleased\n", "## Unreleased\n\n- Still waiting.\n"))
    commit_all(r)
    res = results(r)
    assert rc.WARN in levels(res, "changelog") and rc.exit_code(res) == 0


def test_missing_changelog_fails(tmp_path):
    r = make_repo(tmp_path)
    (r / "CHANGELOG.md").unlink()
    commit_all(r)
    res = results(r)
    assert rc.FAIL in levels(res, "changelog") and rc.FAIL in levels(res, "notes")


# ── 4. updater key ───────────────────────────────────────────────────────────


def test_wrong_key_id_fails(tmp_path):
    r = make_repo(tmp_path)
    edit_json(r / "desktop/src-tauri/tauri.conf.json",
              lambda d: d["plugins"]["updater"].update(pubkey=fake_pubkey("D68774E13CB63DED")))
    commit_all(r)
    res = results(r)
    bad = [x for x in by(res, "updater-key") if x.level == rc.FAIL]
    assert bad and "D68774E13CB63DED" in bad[0].message and rc.EXPECTED_KEY_ID in bad[0].message


def test_comment_that_lies_about_the_key_id_fails(tmp_path):
    r = make_repo(tmp_path)
    forged = fake_pubkey("D68774E13CB63DED", comment_keyid=rc.EXPECTED_KEY_ID)
    edit_json(r / "desktop/src-tauri/tauri.conf.json", lambda d: d["plugins"]["updater"].update(pubkey=forged))
    commit_all(r)
    assert rc.FAIL in levels(results(r), "updater-key")


@pytest.mark.parametrize("bad", ["", "not base64 at all!!", base64.b64encode(b"short").decode()])
def test_garbage_pubkey_fails(tmp_path, bad):
    r = make_repo(tmp_path)
    edit_json(r / "desktop/src-tauri/tauri.conf.json", lambda d: d["plugins"]["updater"].update(pubkey=bad))
    commit_all(r)
    assert rc.FAIL in levels(results(r), "updater-key")


def test_pinned_key_id_file_must_agree(tmp_path):
    r = make_repo(tmp_path)
    (r / "desktop/UPDATER_KEY_ID").write_text("D68774E13CB63DED\n")
    commit_all(r)
    assert rc.FAIL in levels(results(r), "updater-key")
    (r / "desktop/UPDATER_KEY_ID").unlink()
    commit_all(r)
    assert levels(results(r), "updater-key") == {rc.OK, rc.WARN}


def test_the_real_repo_pubkey_is_the_expected_key():
    """The checked-in config, read through the same code path."""
    res = rc.check_updater_key(ROOT)
    assert [x.level for x in res] == [rc.OK, rc.OK], res


# ── 5/6. tauri.conf.json ─────────────────────────────────────────────────────


def test_minimum_system_version(tmp_path):
    r = make_repo(tmp_path)
    conf = r / "desktop/src-tauri/tauri.conf.json"
    for value in ("14.0", "13", "12.0"):
        edit_json(conf, lambda d, v=value: d["bundle"]["macOS"].update(minimumSystemVersion=v))
        commit_all(r)
        assert levels(results(r), "macos") == {rc.FAIL}, value
    edit_json(conf, lambda d: d["bundle"].pop("macOS"))
    commit_all(r)
    assert levels(results(r), "macos") == {rc.FAIL}


@pytest.mark.parametrize("endpoint", [
    "https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-x86_64.json",
    "https://github.com/EVMCOD/conductloop/releases/latest/download/update-macos-aarch64.json",
    "https://github.com/EVMCOD/conductloop-releases/releases/download/v1.2.1/update-macos-aarch64.json",
    "http://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json",
    "https://example.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json",
])
def test_wrong_endpoint_fails(tmp_path, endpoint):
    r = make_repo(tmp_path)
    edit_json(r / "desktop/src-tauri/tauri.conf.json",
              lambda d: d["plugins"]["updater"].update(endpoints=[endpoint]))
    commit_all(r)
    assert levels(results(r), "endpoint") == {rc.FAIL}


def test_extra_endpoint_warns(tmp_path):
    r = make_repo(tmp_path)
    edit_json(r / "desktop/src-tauri/tauri.conf.json",
              lambda d: d["plugins"]["updater"].update(endpoints=[rc.EXPECTED_ENDPOINT, "https://example.com/u.json"]))
    commit_all(r)
    assert levels(results(r), "endpoint") == {rc.OK, rc.WARN}


def test_the_real_repo_config_passes_the_config_checks():
    assert {x.level for x in rc.check_min_macos(ROOT)} == {rc.OK}
    assert {x.level for x in rc.check_endpoint(ROOT)} == {rc.OK}


# ── 7. git ───────────────────────────────────────────────────────────────────


def test_dirty_tree_fails(tmp_path):
    r = make_repo(tmp_path)
    (r / "CHANGELOG.md").write_text((r / "CHANGELOG.md").read_text() + "\n- edited\n")
    res = results(r)
    assert rc.FAIL in levels(res, "git") and "CHANGELOG.md" in by(res, "git")[0].message
    assert rc.exit_code(res) == 1


def test_untracked_file_fails(tmp_path):
    r = make_repo(tmp_path)
    (r / "stray.txt").write_text("x")
    assert rc.FAIL in levels(results(r), "git")


def test_staged_change_fails(tmp_path):
    r = make_repo(tmp_path)
    (r / "x.txt").write_text("x")
    git(r, "add", "x.txt")
    assert rc.FAIL in levels(results(r), "git")


def test_not_a_git_repo_fails(tmp_path):
    r = make_repo(tmp_path)
    import shutil
    shutil.rmtree(r / ".git")
    res = results(r)
    # tmp_path itself is not inside another repository, so this is not a work tree
    assert rc.FAIL in levels(res, "git")


def test_other_branch_and_existing_tag_only_warn(tmp_path):
    r = make_repo(tmp_path)
    git(r, "checkout", "-q", "-b", "feature/x")
    git(r, "tag", f"v{VERSION}")
    res = results(r)
    msgs = [x.message for x in by(res, "git") if x.level == rc.WARN]
    assert len(msgs) == 2 and rc.exit_code(res) == 0


# ── dry-run guarantees ───────────────────────────────────────────────────────


def test_running_the_check_changes_nothing(tmp_path):
    r = make_repo(tmp_path)
    before = (git(r, "rev-parse", "HEAD"), git(r, "status", "--porcelain"), git(r, "tag", "--list"), git(r, "branch", "--list"))
    snapshot = {p: p.read_bytes() for p in r.rglob("*") if p.is_file() and ".git" not in p.parts}
    subprocess.run(["python3", str(SCRIPT), "--version", VERSION, "--root", str(r)],
                   check=True, capture_output=True, env={**os.environ, **GIT_ENV})
    after = (git(r, "rev-parse", "HEAD"), git(r, "status", "--porcelain"), git(r, "tag", "--list"), git(r, "branch", "--list"))
    assert before == after
    assert snapshot == {p: p.read_bytes() for p in r.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_script_has_no_network_and_only_read_only_git():
    src = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    for banned in ("urllib.request", "http.client", "requests", "socket", "smtplib", "ftplib", "os.system", "shell=True"):
        assert banned not in code, f"release_check.py must not use {banned}"
    git_verbs = set(re.findall(r'_git\(\s*root\s*,\s*"([a-z-]+)"', code))
    assert git_verbs <= {"status", "rev-parse", "tag"}, git_verbs
    # `git tag` is only ever called with --list
    assert all(m == "--list" for m in re.findall(r'_git\(root, "tag", "(--?\w+)"', code))
    assert not re.search(r"\b(push|release create|gh )\b", code)


def test_exit_code_helper():
    ok, warn, fail = rc.Result(rc.OK, "a", ""), rc.Result(rc.WARN, "a", ""), rc.Result(rc.FAIL, "a", "")
    assert rc.exit_code([ok]) == 0
    assert rc.exit_code([ok, warn]) == 0
    assert rc.exit_code([ok, warn], strict=True) == 1
    assert rc.exit_code([ok, fail]) == 1
