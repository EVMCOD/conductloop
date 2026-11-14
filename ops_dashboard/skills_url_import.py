"""Import a published agentskills.io skill by URL (gap 3, step 2).

Two phases:

1. Fetch the skill into a sandbox staging directory *outside* the vault,
   validate it with the step-1 folder importer, and return a review payload
   (frontmatter, body, file list with sizes and hashes, warnings, source URL
   and a review hash).
2. An explicit approve, by staging id and with the same review hash, copies
   the folder into the vault and reindexes. Discard deletes the staging
   directory. Staging expires.

HTTPS only. Private, loopback and link-local addresses are refused (including
after redirects). No auth tokens are sent. Scripts are never executed.
Fetching is injected in tests so the suite never opens a socket.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import tarfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from . import app_paths
from .skills.catalog import SkillError
from .skills.import_folder import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_FILES,
    MAX_REL_DEPTH,
    import_from_folder,
    plan_import,
)
from .skills.parse import parse_skill_file

MAX_URL_LEN = 2000
MAX_REDIRECTS = 3
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
CONNECT_TIMEOUT = 15
STAGING_TTL_SEC = 30 * 60
USER_AGENT = "ConductLoop-skill-import"
STAGING_SUBDIR = "skill-import-staging"

# github.com/<owner>/<repo>/(tree|blob)/<ref>/<path>
_GITHUB = re.compile(
    r"^https://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/"
    r"(?P<kind>tree|blob)/"
    r"(?P<rest>.+)$"
)
_ARCHIVE_SUFFIX = (".zip", ".tar.gz", ".tgz")

Resolver = Callable[[str], list[str]]
# Returns (status, headers, body). Must not follow redirects.
Opener = Callable[[str], tuple[int, dict[str, str], bytes]]


def _default_resolver(host: str) -> list[str]:
    return [str(ai[4][0]) for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def _is_blocked_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        (not ip.is_global)
        or ip.is_multicast
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url(url: Any, *, resolver: Resolver | None = None) -> None:
    """Refuse anything that is not a public https URL (SSRF guard)."""
    if not isinstance(url, str) or not url.strip():
        raise SkillError("bad_url", "url is required and must be a string")
    url = url.strip()
    if len(url) > MAX_URL_LEN:
        raise SkillError("bad_url", "url is too long")
    u = urlparse(url)
    if u.scheme == "http":
        raise SkillError("https_only", "only https URLs are accepted")
    if u.scheme != "https" or not u.hostname:
        raise SkillError("https_only", "only https URLs are accepted")
    if u.username is not None or u.password is not None:
        raise SkillError("credentials_refused", "the URL must not contain credentials")
    host = u.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        raise SkillError("host_not_public", f"{host} is not a public address")
    # Literal IP in the host: check it directly, still without connecting.
    try:
        literal_ip = True
        blocked = _is_blocked_ip(host)
    except ValueError:
        literal_ip = False
        blocked = False
    if literal_ip:
        if blocked:
            raise SkillError("host_not_public", f"{host} is not a public address")
        return
    resolve = resolver or _default_resolver
    try:
        addrs = resolve(host)
    except OSError as e:
        raise SkillError("dns_failed", f"could not resolve {host}") from e
    if not addrs:
        raise SkillError("dns_failed", f"could not resolve {host}")
    for a in addrs:
        if _is_blocked_ip(a):
            raise SkillError("host_not_public", f"{host} resolves to a non-public address")


def _default_opener(url: str) -> tuple[int, dict[str, str], bytes]:
    req = Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=CONNECT_TIMEOUT) as resp:  # noqa: S310 - check_url ran first
            buf = bytearray()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_DOWNLOAD_BYTES:
                    raise SkillError("download_too_large", f"download is over {MAX_DOWNLOAD_BYTES} bytes")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), headers, bytes(buf)
    except SkillError:
        raise
    except HTTPError as e:
        headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        body = b""
        try:
            body = e.read(MAX_DOWNLOAD_BYTES + 1)
        except Exception:
            body = b""
        if 300 <= int(e.code) < 400:
            return int(e.code), headers, body
        raise SkillError("http_error", f"download failed with HTTP {e.code}") from e
    except URLError as e:
        raise SkillError("download_failed", "the download failed") from e
    except TimeoutError as e:
        raise SkillError("timeout", "the download timed out") from e


def fetch_url(url: str, *, opener: Opener | None = None, resolver: Resolver | None = None) -> bytes:
    """GET an https URL, following a few redirects, re-checking each hop."""
    current = url
    do_open = opener or _default_opener
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current, resolver=resolver)
        status, headers, body = do_open(current)
        if status in (301, 302, 303, 307, 308):
            loc = (headers.get("location") or "").strip()
            if not loc:
                raise SkillError("redirect_refused", "redirect with no Location")
            current = urljoin(current, loc)
            continue
        if status >= 400:
            raise SkillError("http_error", f"download failed with HTTP {status}")
        if len(body) > MAX_DOWNLOAD_BYTES:
            raise SkillError("download_too_large", f"download is over {MAX_DOWNLOAD_BYTES} bytes")
        return body
    raise SkillError("too_many_redirects", f"more than {MAX_REDIRECTS} redirects")


@dataclass
class ParsedSource:
    kind: str  # "github" | "archive"
    url: str
    owner: str = ""
    repo: str = ""
    ref: str = ""
    subpath: str = ""  # skill folder inside the archive, posix, no leading slash


def _clean_subpath(path: str) -> str:
    path = path.strip().strip("/")
    if not path:
        return ""
    parts = [p for p in path.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise SkillError("unsupported_url", "the URL path is not allowed")
    return "/".join(parts)


def classify_url(url: str) -> ParsedSource:
    if not isinstance(url, str) or not url.strip():
        raise SkillError("bad_url", "url is required and must be a string")
    raw = url.strip()
    # HTTPS / public-IP checks happen in fetch_url / preview (so classify can
    # be unit-tested with fixtures). Scheme is still filtered here.
    u = urlparse(raw)
    if u.scheme == "http":
        raise SkillError("https_only", "only https URLs are accepted")
    if u.scheme != "https":
        raise SkillError("https_only", "only https URLs are accepted")

    m = _GITHUB.match(raw.split("?", 1)[0].split("#", 1)[0])
    if m:
        rest = _clean_subpath(m.group("rest"))
        if not rest:
            raise SkillError("unsupported_url", "the GitHub URL must include a ref")
        # First path segment is the ref (branches with slashes should use a
        # direct archive URL instead).
        bits = rest.split("/", 1)
        ref, tail = bits[0], bits[1] if len(bits) == 2 else ""
        if m.group("kind") == "blob":
            if tail.endswith("/SKILL.md") or tail == "SKILL.md":
                tail = tail[: -len("SKILL.md")].rstrip("/")
            elif tail.endswith("SKILL.md"):
                tail = tail[: -len("SKILL.md")].rstrip("/")
        return ParsedSource(
            kind="github",
            url=raw.split("?", 1)[0].split("#", 1)[0],
            owner=m.group("owner"),
            repo=m.group("repo"),
            ref=ref,
            subpath=_clean_subpath(tail),
        )

    path = (u.path or "").lower()
    if any(path.endswith(s) for s in _ARCHIVE_SUFFIX):
        return ParsedSource(kind="archive", url=raw.split("#", 1)[0])
    raise SkillError(
        "unsupported_url",
        "url must be a GitHub tree/blob address or a direct .zip / .tar.gz / .tgz address",
    )


def github_tarball_url(src: ParsedSource) -> str:
    return f"https://codeload.github.com/{src.owner}/{src.repo}/tar.gz/{src.ref}"


# ---------- safe archive extraction ----------

def _safe_rel(name: str) -> str:
    raw = name.replace("\\", "/")
    if raw.startswith("/") or raw.startswith("\\\\") or (len(raw) > 1 and raw[1] == ":"):
        raise SkillError("unsafe_archive", f"absolute path in archive: {name[:80]}")
    parts = [p for p in raw.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise SkillError("unsafe_archive", f"path traversal in archive: {name[:80]}")
    if not parts:
        raise SkillError("unsafe_archive", "empty path in archive")
    if len(parts) > MAX_REL_DEPTH + 2:  # allow a tarball root prefix + skill depth
        raise SkillError("too_deep", f"{'/'.join(parts)} is nested too deep")
    return "/".join(parts)


def _write_extracted(dest_root: Path, rel: str, data: bytes, total: list[int], files: list[str]) -> None:
    if rel.endswith("/"):
        return
    safe = _safe_rel(rel)
    if any(p.startswith(".") for p in safe.split("/")):
        return
    total[0] += len(data)
    files.append(safe)
    if len(files) > MAX_IMPORT_FILES or total[0] > MAX_IMPORT_BYTES:
        raise SkillError(
            "too_large",
            f"the skill folder is too large (at most {MAX_IMPORT_FILES} files "
            f"and {MAX_IMPORT_BYTES} bytes)",
        )
    target = dest_root / Path(safe)
    try:
        target.resolve().relative_to(dest_root.resolve())
    except ValueError as e:
        raise SkillError("unsafe_archive", f"path traversal in archive: {rel[:80]}") from e
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def extract_archive(data: bytes, dest_root: Path) -> None:
    dest_root.mkdir(parents=True, exist_ok=True)
    total = [0]
    files: list[str] = []
    if len(data) >= 4 and data[:4] == b"PK\x03\x04" or (len(data) >= 2 and data[:2] == b"PK"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise SkillError("bad_archive", "the file is not a valid zip archive") from e
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    _safe_rel(info.filename)
                    continue
                # Symlinks in zip: Unix extra / external attrs (0xA000 is symlink).
                is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
                if is_link or info.filename.endswith("/"):
                    if is_link:
                        raise SkillError("unsafe_archive", f"symlink in archive: {info.filename[:80]}")
                    continue
                if info.file_size > MAX_IMPORT_BYTES:
                    raise SkillError("too_large", "a file in the archive is too large")
                with zf.open(info, "r") as fh:
                    payload = fh.read(MAX_IMPORT_BYTES + 1)
                if len(payload) > MAX_IMPORT_BYTES:
                    raise SkillError("too_large", "a file in the archive is too large")
                _write_extracted(dest_root, info.filename, payload, total, files)
        if not files:
            raise SkillError("bad_archive", "the archive is empty")
        return

    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as e:
        raise SkillError("bad_archive", "the file is not a valid zip or tar.gz archive") from e
    with tf:
        for member in tf.getmembers():
            rel = member.name
            if member.issym() or member.islnk():
                raise SkillError("unsafe_archive", f"symlink in archive: {rel[:80]}")
            if member.isdir():
                if rel:
                    _safe_rel(rel)
                continue
            if not member.isfile():
                raise SkillError("unsafe_archive", f"unsupported archive entry: {rel[:80]}")
            if member.size > MAX_IMPORT_BYTES:
                raise SkillError("too_large", "a file in the archive is too large")
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            payload = extracted.read(MAX_IMPORT_BYTES + 1)
            if len(payload) > MAX_IMPORT_BYTES:
                raise SkillError("too_large", "a file in the archive is too large")
            _write_extracted(dest_root, rel, payload, total, files)
    if not files:
        raise SkillError("bad_archive", "the archive is empty")


def _strip_tarball_prefix(root: Path) -> Path:
    """GitHub tarballs have a single top-level '<repo>-<ref>/' directory."""
    entries = [p for p in root.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def _find_skill_dir(root: Path, subpath: str) -> Path:
    base = _strip_tarball_prefix(root)
    if subpath:
        d = base.joinpath(*subpath.split("/"))
        if not d.is_dir() or d.is_symlink():
            raise SkillError("missing_skill_md", f"no skill folder at {subpath}")
        return d
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "SKILL.md" in filenames:
            found.append(Path(dirpath))
            if len(found) > 8:
                break
    if len(found) == 1:
        return found[0]
    if not found:
        raise SkillError("missing_skill_md", "the archive has no SKILL.md")
    raise SkillError("ambiguous_skill", "the archive contains more than one SKILL.md; use a GitHub tree URL that points at one skill folder")


# ---------- staging ----------

def staging_root() -> Path:
    return app_paths.data_dir() / STAGING_SUBDIR


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _purge_expired(now: float | None = None) -> None:
    root = staging_root()
    if not root.is_dir():
        return
    t = _now(now)
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        meta = d / "meta.json"
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            exp = float(data.get("expires_at") or 0)
        except (OSError, ValueError, TypeError):
            shutil.rmtree(d, ignore_errors=True)
            continue
        if exp and exp <= t:
            shutil.rmtree(d, ignore_errors=True)


def _meta_path(sid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", sid or ""):
        raise SkillError("not_found", "unknown or expired import", 404)
    return staging_root() / sid / "meta.json"


def _read_meta(sid: str, now: float | None = None) -> dict[str, Any]:
    path = _meta_path(sid)
    if not path.is_file() or path.is_symlink():
        _purge_expired(now)
        raise SkillError("not_found", "unknown or expired import", 404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SkillError("not_found", "unknown or expired import", 404) from e
    if float(data.get("expires_at") or 0) <= _now(now):
        shutil.rmtree(path.parent, ignore_errors=True)
        _purge_expired(now)
        raise SkillError("expired", "this import preview has expired; fetch it again", 404)
    _purge_expired(now)
    return data


def _file_listing(skill_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in sorted(filenames):
            if f.startswith("."):
                continue
            fp = Path(dirpath) / f
            if fp.is_symlink() or not fp.is_file():
                continue
            rel = fp.relative_to(skill_dir).as_posix()
            data = fp.read_bytes()
            out.append({
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    out.sort(key=lambda r: r["path"])
    return out


def _review_hash(source_url: str, files: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    h.update(source_url.encode("utf-8"))
    h.update(b"\n")
    for f in files:
        h.update(f["path"].encode("utf-8"))
        h.update(b"\0")
        h.update(f["sha256"].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _has_scripts(skill_dir: Path) -> bool:
    for dirpath, _dirnames, filenames in os.walk(skill_dir, followlinks=False):
        rel = Path(dirpath).relative_to(skill_dir)
        if rel.parts and rel.parts[0] == "scripts":
            return True
        if any(f.startswith("scripts/") for f in filenames):
            return True
    return False


def _warnings(skill_dir: Path, plan_warnings: list[tuple[str, str]]) -> list[dict[str, str]]:
    out = [{"code": c, "message": m} for c, m in plan_warnings]
    if _has_scripts(skill_dir):
        out.append({
            "code": "scripts_present",
            "message": "this folder includes scripts; they will be copied as files and never run",
        })
    return out


def preview_from_url(
    url: Any,
    *,
    opener: Opener | None = None,
    resolver: Resolver | None = None,
    fetch: Callable[[str], bytes] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Phase 1: download into staging, validate, return a review payload."""
    if not isinstance(url, str):
        raise SkillError("bad_url", "url is required and must be a string")
    src = classify_url(url)
    check_url(src.url, resolver=resolver)
    if src.kind == "github":
        download_url = github_tarball_url(src)
        check_url(download_url, resolver=resolver)
    else:
        download_url = src.url

    if fetch is not None:
        blob = fetch(download_url)
    else:
        blob = fetch_url(download_url, opener=opener, resolver=resolver)
    if len(blob) > MAX_DOWNLOAD_BYTES:
        raise SkillError("download_too_large", f"download is over {MAX_DOWNLOAD_BYTES} bytes")

    _purge_expired(now)
    sid = hashlib.sha256(os.urandom(32)).hexdigest()[:24]
    work = staging_root() / sid
    extract_dir = work / "extract"
    try:
        extract_archive(blob, extract_dir)
        skill_src = _find_skill_dir(extract_dir, src.subpath)
        plan = plan_import(str(skill_src.resolve()))
        # Place the skill under a directory named after it (step-1 rule).
        skill_dest = work / plan.name
        if skill_dest.exists():
            shutil.rmtree(skill_dest)
        shutil.copytree(skill_src, skill_dest, symlinks=False)
        # Re-validate the final folder (name matches directory).
        plan = plan_import(str(skill_dest))
        files = _file_listing(skill_dest)
        body = parse_skill_file(skill_dest / "SKILL.md").body
        review = _review_hash(src.url, files)
        created = _now(now)
        warnings = _warnings(skill_dest, plan.warnings)
        payload = {
            "ok": True,
            "id": sid,
            "hash": review,
            "source_url": src.url,
            "name": plan.name,
            "description": plan.description,
            "body": body,
            "files": files,
            "warnings": warnings,
            "expires_at": int(created + STAGING_TTL_SEC),
        }
        (work / "meta.json").write_text(json.dumps({
            **payload,
            "skill_dir": plan.name,
            "created_at": created,
        }, ensure_ascii=False), encoding="utf-8")
        # Drop the raw extract so only the reviewed tree remains.
        shutil.rmtree(extract_dir, ignore_errors=True)
        return payload
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def approve(staging_id: Any, review_hash: Any, *, now: float | None = None) -> dict[str, Any]:
    """Phase 2: copy the reviewed staging folder into the vault."""
    if not isinstance(staging_id, str) or not staging_id.strip():
        raise SkillError("not_found", "unknown or expired import", 404)
    if not isinstance(review_hash, str) or not review_hash.strip():
        raise SkillError("hash_mismatch", "the review hash is required and must match")
    meta = _read_meta(staging_id.strip(), now)
    if meta.get("hash") != review_hash.strip():
        raise SkillError("hash_mismatch", "the review hash does not match this preview")
    work = staging_root() / staging_id.strip()
    skill_dir = work / str(meta.get("skill_dir") or "")
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        raise SkillError("not_found", "unknown or expired import", 404)
    files = _file_listing(skill_dir)
    current = _review_hash(str(meta.get("source_url") or ""), files)
    if current != review_hash.strip() or current != meta.get("hash"):
        raise SkillError("hash_mismatch", "the staged files changed since they were reviewed")
    result = import_from_folder(str(skill_dir))
    shutil.rmtree(work, ignore_errors=True)
    result["source_url"] = meta.get("source_url")
    result["warnings"] = list(result.get("warnings") or []) + [
        w for w in (meta.get("warnings") or []) if w.get("code") == "scripts_present"
    ]
    return result


def discard(staging_id: Any, *, now: float | None = None) -> dict[str, Any]:
    if not isinstance(staging_id, str) or not staging_id.strip():
        raise SkillError("not_found", "unknown or expired import", 404)
    # Unknown ids are a 404; expired ones are cleaned and also 404.
    try:
        meta = _read_meta(staging_id.strip(), now)
    except SkillError:
        raise
    work = staging_root() / staging_id.strip()
    shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "discarded": True, "id": staging_id.strip(), "name": meta.get("name")}
