"""Writes for approved skill proposals (PR L6). Only ever called from
``propose.approve`` — i.e. after the user approved a proposal.

* create  → ``<vault>/skills/<name>/`` built in a temp dir, then ``rename``.
* patch   → only if SKILL.md still hashes to the proposal's ``base_hash``;
            each file is written to a temp file and ``os.replace``d.
* archive → the directory is moved to ``<vault>/skills/.archive/`` (D14).
* delete  → the directory is moved to ``<vault>/skills/.trash/``.

No skill directory is ever ``rmtree``d: archive and delete can be undone by moving the
directory back. Dot-directories are skipped by the indexer, so moved skills
leave the catalog. Only the vault skills root is ever written to.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .catalog import SkillError, vault_dir

ARCHIVE_DIR = ".archive"
TRASH_DIR = ".trash"


def skill_dir(name: str) -> Path:
    root = vault_dir()
    d = root / name
    if d.parent != root or name.startswith(".") or "/" in name or "\\" in name:
        raise SkillError("bad_name", "invalid skill name")
    return d


def current_hash(name: str) -> str | None:
    f = skill_dir(name) / "SKILL.md"
    if f.is_symlink() or not f.is_file():
        return None
    return hashlib.sha256(f.read_bytes()).hexdigest()


def current_text(name: str) -> str | None:
    f = skill_dir(name) / "SKILL.md"
    if f.is_symlink() or not f.is_file():
        return None
    return f.read_text(encoding="utf-8", errors="replace")


def render(fm: dict[str, Any], body: str) -> str:
    import yaml  # PyYAML (D8); safe_dump only
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)
    return f"---\n{head}---\n\n{body.strip()}\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_dir(d: Path) -> None:
    if d.is_symlink() or not d.is_dir():
        raise SkillError("not_found", f"skill directory {d.name} does not exist in the vault", 404)


def write_create(name: str, skill_md: str, files: dict[str, str]) -> Path:
    root = vault_dir()
    dest = skill_dir(name)
    if dest.exists() or dest.is_symlink():
        raise SkillError("exists", f"a skill named '{name}' already exists; propose a patch instead", 409)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{name}.new-", dir=root))
    try:
        (tmp / "SKILL.md").write_text(skill_md, encoding="utf-8")
        for rel, text in files.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        os.rename(tmp, dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)  # our own temp dir only
        raise
    return dest


def write_patch(name: str, skill_md: str, files: dict[str, str], base_hash: str | None) -> Path:
    d = skill_dir(name)
    _check_dir(d)
    if not base_hash or current_hash(name) != base_hash:
        raise SkillError("stale_base", "the skill changed since this patch was proposed; propose it again", 409)
    for rel in files:
        target = d / rel
        if target.is_symlink():
            raise SkillError("symlink", f"{rel} is a symlink; refusing to write", 409)
    for rel, text in files.items():
        _write_atomic(d / rel, text)
    _write_atomic(d / "SKILL.md", skill_md)
    return d


def _move_out(name: str, sub: str) -> Path:
    d = skill_dir(name)
    _check_dir(d)
    target_root = vault_dir() / sub
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    n = 1
    while target.exists():
        n += 1
        target = target_root / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    os.rename(d, target)
    return target


def archive(name: str) -> Path:
    return _move_out(name, ARCHIVE_DIR)


def trash(name: str) -> Path:
    return _move_out(name, TRASH_DIR)
