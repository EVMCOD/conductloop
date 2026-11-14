"""Gap 3 step 2: import a published skill by URL, after review.

Every test is offline: DNS and HTTP are injected (the default opener and
resolver raise if they are ever called). No test marked live is collected
by the default `not live` run.
"""
from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from ops_dashboard import skills_url_import as U
from ops_dashboard.skills.catalog import SkillError
from ops_dashboard.skills.import_folder import import_from_folder

PUBLIC = "1.1.1.1"


def _public_dns(host: str) -> list[str]:
    return [PUBLIC]


def _skill_text(name: str, description: str = "What it does and when to use it.",
                body: str = "# Hello\n\nDo the thing.\n", extra_fm: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra_fm}---\n\n{body}"


def _zip_skill(name: str, *, helpers: dict[str, bytes] | None = None,
               root: str | None = None) -> bytes:
    prefix = f"{root}/{name}" if root else name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{prefix}/SKILL.md", _skill_text(name))
        for rel, data in (helpers or {}).items():
            zf.writestr(f"{prefix}/{rel}", data)
    return buf.getvalue()


def _tar_skill(name: str, *, helpers: dict[str, bytes] | None = None,
               root: str = "skills-main", symlink: str | None = None,
               traversal: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(rel: str, data: bytes) -> None:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        add(f"{root}/{name}/SKILL.md", _skill_text(name).encode())
        for rel, data in (helpers or {}).items():
            add(f"{root}/{name}/{rel}", data)
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        if traversal:
            add(traversal, b"escaped\n")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The suite must never open a socket or resolve a real name."""

    def boom_dns(host: str) -> list[str]:
        raise AssertionError(f"DNS used for {host}")

    def boom_open(url: str):
        raise AssertionError(f"HTTP used for {url}")

    monkeypatch.setattr(U, "_default_resolver", boom_dns)
    monkeypatch.setattr(U, "_default_opener", boom_open)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    db.init_db()
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault" / "skills"
    vault.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPS_SKILLS_DIR", str(vault))
    monkeypatch.setenv("OPS_DATA_DIR", str(data))
    return {"home": home, "vault": vault, "tmp": tmp_path, "data": data}


def _preview(url: str, blob: bytes, **kw):
    return U.preview_from_url(url, fetch=lambda _u: blob, resolver=_public_dns, **kw)


# ---------- classify / https ----------

def test_http_is_rejected():
    with pytest.raises(SkillError) as ei:
        U.classify_url("http://example.com/skill.zip")
    assert ei.value.code == "https_only"
    with pytest.raises(SkillError) as ei:
        U.check_url("http://github.com/o/r/tree/main/s", resolver=_public_dns)
    assert ei.value.code == "https_only"


def test_github_and_archive_urls_parse():
    g = U.classify_url("https://github.com/example-org/skills/tree/main/pdf-summarise")
    assert g.kind == "github" and g.owner == "example-org" and g.repo == "skills"
    assert g.ref == "main" and g.subpath == "pdf-summarise"
    assert U.github_tarball_url(g) == "https://codeload.github.com/example-org/skills/tar.gz/main"
    b = U.classify_url("https://github.com/example-org/skills/blob/main/pdf-summarise/SKILL.md")
    assert b.subpath == "pdf-summarise" and b.ref == "main"
    a = U.classify_url("https://example.com/published/pdf-summarise.tar.gz")
    assert a.kind == "archive"
    with pytest.raises(SkillError) as ei:
        U.classify_url("https://example.com/not-an-archive")
    assert ei.value.code == "unsupported_url"


# ---------- SSRF ----------

def test_ssrf_blocked_direct_ip():
    for url in (
        "https://127.0.0.1/skill.zip",
        "https://10.0.0.8/skill.zip",
        "https://192.168.1.1/skill.zip",
        "https://169.254.169.254/latest/skill.zip",
        "https://[::1]/skill.zip",
        "https://[::ffff:127.0.0.1]/skill.zip",
    ):
        with pytest.raises(SkillError) as ei:
            U.check_url(url)
        assert ei.value.code == "host_not_public", (url, ei.value.code)


def test_ssrf_blocked_hostname_and_localhost():
    with pytest.raises(SkillError) as ei:
        U.check_url("https://localhost/skill.zip")
    assert ei.value.code == "host_not_public"
    with pytest.raises(SkillError) as ei:
        U.check_url("https://metadata.internal/skill.zip",
                    resolver=lambda _h: ["169.254.169.254"])
    assert ei.value.code == "host_not_public"
    with pytest.raises(SkillError) as ei:
        U.check_url("https://intranet.example/skill.zip",
                    resolver=lambda _h: ["10.1.2.3"])
    assert ei.value.code == "host_not_public"


def test_ssrf_blocked_after_redirect():
    hops = {"https://example.com/skill.zip": (
        302, {"location": "https://127.0.0.1/secret.zip"}, b"",
    )}

    def opener(url: str):
        return hops[url]

    with pytest.raises(SkillError) as ei:
        U.fetch_url("https://example.com/skill.zip", opener=opener, resolver=_public_dns)
    assert ei.value.code == "host_not_public"

    hops["https://example.com/skill.zip"] = (
        302, {"location": "http://example.com/skill.zip"}, b"",
    )
    with pytest.raises(SkillError) as ei:
        U.fetch_url("https://example.com/skill.zip", opener=opener, resolver=_public_dns)
    assert ei.value.code == "https_only"

    hops.clear()
    hops["https://example.com/a.zip"] = (302, {"location": "https://example.com/b.zip"}, b"")
    hops["https://example.com/b.zip"] = (302, {"location": "https://example.com/c.zip"}, b"")
    hops["https://example.com/c.zip"] = (302, {"location": "https://example.com/d.zip"}, b"")
    hops["https://example.com/d.zip"] = (302, {"location": "https://example.com/e.zip"}, b"")
    with pytest.raises(SkillError) as ei:
        U.fetch_url("https://example.com/a.zip", opener=opener, resolver=_public_dns)
    assert ei.value.code == "too_many_redirects"


def test_credentials_in_url_are_refused():
    with pytest.raises(SkillError) as ei:
        U.check_url("https://user:ghp_secret@github.com/o/r/tree/main/s", resolver=_public_dns)
    assert ei.value.code == "credentials_refused"


# ---------- happy paths ----------

def test_happy_path_github_tarball(env):
    blob = _tar_skill("pdf-summarise", helpers={"references/notes.md": b"# notes\n"})
    fetched = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return blob

    url = "https://github.com/example-org/skills/tree/main/pdf-summarise"
    review = U.preview_from_url(url, fetch=fetch, resolver=_public_dns)
    assert review["ok"] and review["name"] == "pdf-summarise"
    assert review["id"] and review["hash"]
    assert review["source_url"].startswith("https://github.com/")
    assert fetched == ["https://codeload.github.com/example-org/skills/tar.gz/main"]
    paths = {f["path"]: f for f in review["files"]}
    assert "SKILL.md" in paths and "references/notes.md" in paths
    assert paths["references/notes.md"]["size"] == 8
    assert len(paths["references/notes.md"]["sha256"]) == 64
    assert "Do the thing." in review["body"]
    # still only in staging, not the vault
    assert not (env["vault"] / "pdf-summarise").exists()
    staging = env["data"] / "skill-import-staging" / review["id"]
    assert staging.is_dir() and not (env["data"] / "vault").exists()

    result = U.approve(review["id"], review["hash"])
    assert result["ok"] and result["key"] == "pdf-summarise"
    assert (env["vault"] / "pdf-summarise" / "SKILL.md").is_file()
    assert (env["vault"] / "pdf-summarise" / "references" / "notes.md").read_bytes() == b"# notes\n"
    assert not staging.exists()


def test_happy_path_zip(env):
    blob = _zip_skill("zip-skill", helpers={"scripts/hello.sh": b"#!/bin/sh\necho hi\n"})
    url = "https://example.com/published/zip-skill.zip"
    review = _preview(url, blob)
    assert review["name"] == "zip-skill"
    assert any(w["code"] == "scripts_present" for w in review["warnings"])
    result = U.approve(review["id"], review["hash"])
    dest = Path(result["path"])
    assert dest == env["vault"] / "zip-skill"
    assert (dest / "scripts" / "hello.sh").read_bytes().startswith(b"#!/bin/sh")


# ---------- review / approve gates ----------

def test_review_hash_mismatch_is_rejected(env):
    review = _preview("https://example.com/pdf-summarise.zip", _zip_skill("pdf-summarise"))
    with pytest.raises(SkillError) as ei:
        U.approve(review["id"], "0" * 64)
    assert ei.value.code == "hash_mismatch"
    assert not (env["vault"] / "pdf-summarise").exists()
    # staging still there so the user can retry with the right hash
    assert (env["data"] / "skill-import-staging" / review["id"]).is_dir()


def test_approve_without_review_is_rejected(env):
    with pytest.raises(SkillError) as ei:
        U.approve("does-not-exist-xxxxx", "a" * 64)
    assert ei.value.code == "not_found"
    with pytest.raises(SkillError) as ei:
        U.approve("", "a" * 64)
    assert ei.value.code == "not_found"


def test_existing_name_is_409_on_approve(env):
    # already in the vault from a local import
    local = env["tmp"] / "pdf-summarise"
    local.mkdir()
    (local / "SKILL.md").write_text(_skill_text("pdf-summarise"))
    import_from_folder(str(local))
    review = _preview("https://example.com/pdf-summarise.zip", _zip_skill("pdf-summarise"))
    with pytest.raises(SkillError) as ei:
        U.approve(review["id"], review["hash"])
    assert ei.value.code == "exists" and ei.value.status == 409


def test_discard_and_expiry(env):
    review = _preview("https://example.com/pdf-summarise.zip", _zip_skill("pdf-summarise"), now=1_000.0)
    U.discard(review["id"], now=1_000.0)
    assert not (env["data"] / "skill-import-staging" / review["id"]).exists()
    with pytest.raises(SkillError) as ei:
        U.approve(review["id"], review["hash"], now=1_001.0)
    assert ei.value.code == "not_found"

    review = _preview("https://example.com/pdf-summarise.zip", _zip_skill("pdf-summarise"), now=1_000.0)
    with pytest.raises(SkillError) as ei:
        U.approve(review["id"], review["hash"], now=1_000.0 + U.STAGING_TTL_SEC + 1)
    assert ei.value.code == "expired"
    assert not (env["data"] / "skill-import-staging" / review["id"]).exists()
    assert not (env["vault"] / "pdf-summarise").exists()


# ---------- archive / size ----------

def test_oversize_download_is_rejected(env):
    too_big = b"PK" + b"x" * (U.MAX_DOWNLOAD_BYTES)
    with pytest.raises(SkillError) as ei:
        U.preview_from_url(
            "https://example.com/huge.zip",
            fetch=lambda _u: too_big,
            resolver=_public_dns,
        )
    assert ei.value.code == "download_too_large"
    assert not any((env["data"] / "skill-import-staging").glob("*")) if (env["data"] / "skill-import-staging").exists() else True


def test_traversal_and_symlink_in_archive_are_rejected(env):
    with pytest.raises(SkillError) as ei:
        U.preview_from_url(
            "https://example.com/bad.tar.gz",
            fetch=lambda _u: _tar_skill("pdf-summarise", traversal="../../evil.md"),
            resolver=_public_dns,
        )
    assert ei.value.code == "unsafe_archive"
    assert not (env["tmp"] / "evil.md").exists()
    assert not list((env["data"] / "skill-import-staging").glob("*")) if (env["data"] / "skill-import-staging").exists() else True

    with pytest.raises(SkillError) as ei:
        U.preview_from_url(
            "https://example.com/link.tar.gz",
            fetch=lambda _u: _tar_skill("pdf-summarise", symlink="pdf-summarise/outside"),
            resolver=_public_dns,
        )
    assert ei.value.code == "unsafe_archive"


def test_default_paths_never_talk_to_the_network():
    src = Path(U.__file__).read_text()
    assert "Authorization" not in src
    assert "GH_TOKEN" not in src and "GITHUB_TOKEN" not in src
    # real HTTP lives only in the default opener, which tests replace
    assert "urlopen" in src and "User-Agent" in src


def test_routes_are_local_only_and_require_review(env, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from ops_dashboard.server import DashboardHandler

    blob = _zip_skill("route-skill")
    monkeypatch.setattr(U, "fetch_url", lambda url, **k: blob)
    # preview_from_url still calls check_url → resolver
    monkeypatch.setattr(U, "_default_resolver", _public_dns)
    # fetch_url is used when fetch= is omitted (the route)

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

    try:
        st, d = req("/api/learning/skills/import-url",
                    {"url": "https://example.com/route-skill.zip"})
        assert st == 200 and d["name"] == "route-skill" and d["id"] and d["hash"]
        assert not (env["vault"] / "route-skill").exists()

        st, d2 = req("/api/learning/skills/import-url/approve",
                     {"id": d["id"], "hash": "deadbeef"})
        assert st == 400 and d2["error"] == "hash_mismatch"

        st, d2 = req("/api/learning/skills/import-url/approve",
                     {"id": "no-such-id", "hash": d["hash"]})
        assert st == 404

        st, d2 = req("/api/learning/skills/import-url/approve",
                     {"id": d["id"], "hash": d["hash"]})
        assert st == 200 and d2["ok"] is True and d2["key"] == "route-skill"
        assert (env["vault"] / "route-skill" / "SKILL.md").is_file()

        st, _ = req("/api/learning/skills/import-url",
                    {"url": "https://example.com/route-skill.zip"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_wires_the_url_review():
    web = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
    html = (web / "learning.html").read_text()
    js = (web / "learning.js").read_text()
    for el_id in ("skills-import-url", "skills-import-url-btn", "skills-url-review",
                  "skills-url-approve", "skills-url-discard"):
        assert f'id="{el_id}"' in html, el_id
    assert "/api/learning/skills/import-url" in js
    assert "/api/learning/skills/import-url/approve" in js
    assert "/api/learning/skills/import-url/discard" in js
