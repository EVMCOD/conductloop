"""Import a local agentskills.io skill folder into the vault (gap 3, step 1).

A skill is a directory that contains ``SKILL.md`` (YAML frontmatter with at
least ``name`` and ``description``, a Markdown body, and optional helper
files). This module validates that folder strictly and copies it into
``<vault>/skills/<name>/`` using the same atomic rename as ``store.write_create``.

Scripts and other helper files are copied as ordinary files and are never
executed. Import by URL (sandbox + review) is step 2 and is not implemented
here. Nothing in this module touches the network.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import parse as skill_parse
from .catalog import SkillError, vault_dir
from .store import skill_dir
from .validate import MAX_DESCRIPTION, MAX_NAME, NAME_RE

# Folder caps, aligned with catalog.export (a skill you cannot export
# should not be imported either). SKILL.md itself is also capped by the parser.
MAX_IMPORT_FILES = 500
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_REL_DEPTH = 4


@dataclass
class ImportPlan:
    name: str
    source: Path
    description: str
    files: list[tuple[Path, str]]  # (absolute source file, relative dest path)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    total_bytes: int = 0


def _as_path(path: Any) -> Path:
    if not isinstance(path, (str, Path)) or (isinstance(path, str) and not path.strip()):
        raise SkillError("bad_path", "path is required and must be a string")
    raw = str(path).strip()
    if len(raw) > 4096:
        raise SkillError("bad_path", "path is too long")
    return Path(raw).expanduser()


def _reject_symlinks_and_escape(root: Path, fp: Path, rel_label: str) -> None:
    if fp.is_symlink():
        raise SkillError("symlink", f"{rel_label} is a symlink; refusing to import")
    try:
        resolved = fp.resolve()
        root_resolved = root.resolve()
    except OSError as e:
        raise SkillError("unreadable", f"{rel_label} cannot be read: {e}") from e
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise SkillError("path_traversal", f"{rel_label} points outside the skill folder") from e


def _rel_of(root: Path, fp: Path) -> str:
    rel = fp.relative_to(root)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise SkillError("path_traversal", f"refusing path {rel}")
    if len(rel.parts) > MAX_REL_DEPTH:
        raise SkillError("too_deep", f"{rel} is nested more than {MAX_REL_DEPTH} levels")
    return rel.as_posix()


def plan_import(path: Any) -> ImportPlan:
    """Validate a local skill folder. Raises SkillError on any blocking problem."""
    src = _as_path(path)
    if not src.is_absolute():
        raise SkillError("bad_path", "path must be an absolute path to a skill folder")
    if src.is_symlink():
        raise SkillError("symlink", "the skill folder is a symlink; refusing to import")
    if src.is_file():
        raise SkillError("not_a_folder", "path must be the skill folder (the directory that contains SKILL.md), not a file")
    if not src.is_dir():
        raise SkillError("not_found", f"no skill folder at {src}", 404)

    skill_md = src / "SKILL.md"
    if skill_md.is_symlink():
        raise SkillError("symlink", "SKILL.md is a symlink; refusing to import")
    if not skill_md.is_file():
        raise SkillError("missing_skill_md", "the folder has no SKILL.md")

    _reject_symlinks_and_escape(src, skill_md, "SKILL.md")

    parsed = skill_parse.parse_skill_file(skill_md)
    if parsed.errors:
        code, msg = parsed.errors[0]
        raise SkillError(code, msg)

    fm = parsed.frontmatter or {}
    warnings: list[tuple[str, str]] = list(parsed.warnings)

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillError("name_missing", "frontmatter is missing a 'name'")
    name = name.strip()
    if len(name) > MAX_NAME or not NAME_RE.match(name):
        raise SkillError(
            "name_format",
            "'name' must be 1-64 lowercase letters, digits and single hyphens, "
            "and must not start or end with a hyphen",
        )
    if name != src.name:
        raise SkillError(
            "name_dir_mismatch",
            f"'name' ({name}) must match the folder name ({src.name})",
        )

    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise SkillError(
            "description_missing",
            "'description' is required (what the skill does and when to use it)",
        )
    if len(desc) > MAX_DESCRIPTION:
        raise SkillError(
            "description_too_long",
            f"'description' must be at most {MAX_DESCRIPTION} characters ({len(desc)} given)",
        )
    if not (parsed.body or "").strip():
        warnings.append(("empty_body", "the Markdown body is empty"))
    if "allowed-tools" in fm:
        warnings.append(("allowed_tools_ignored", "'allowed-tools' is experimental and ignored by ConductLoop"))

    files: list[tuple[Path, str]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        keep: list[str] = []
        for d in dirnames:
            if d.startswith("."):
                continue
            dp = Path(dirpath) / d
            label = _rel_of(src, dp)
            _reject_symlinks_and_escape(src, dp, label)
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f.startswith("."):
                continue
            fp = Path(dirpath) / f
            rel = _rel_of(src, fp)
            if fp.is_symlink():
                raise SkillError("symlink", f"{rel} is a symlink; refusing to import")
            if not fp.is_file():
                raise SkillError("not_a_file", f"{rel} is not a regular file")
            _reject_symlinks_and_escape(src, fp, rel)
            try:
                size = fp.stat().st_size
            except OSError as e:
                raise SkillError("unreadable", f"{rel} cannot be read: {e}") from e
            total += size
            files.append((fp, rel))
            if len(files) > MAX_IMPORT_FILES or total > MAX_IMPORT_BYTES:
                raise SkillError(
                    "too_large",
                    f"the skill folder is too large (at most {MAX_IMPORT_FILES} files "
                    f"and {MAX_IMPORT_BYTES} bytes)",
                )

    if not any(rel == "SKILL.md" for _fp, rel in files):
        raise SkillError("missing_skill_md", "the folder has no SKILL.md")

    return ImportPlan(
        name=name,
        source=src,
        description=desc.strip(),
        files=files,
        warnings=warnings,
        total_bytes=total,
    )


def apply_import(plan: ImportPlan) -> Path:
    """Copy the planned files into the vault. Never executes anything in the folder."""
    root = vault_dir()
    dest = skill_dir(plan.name)
    if dest.exists() or dest.is_symlink():
        raise SkillError("exists", f"a skill named '{plan.name}' already exists in the vault", 409)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{plan.name}.import-", dir=root))
    try:
        for src, rel in plan.files:
            target = tmp / Path(rel)
            # Refuse any destination that would leave the temp dir (belt and braces).
            try:
                target.resolve().relative_to(tmp.resolve())
            except ValueError as e:
                raise SkillError("path_traversal", f"{rel} points outside the skill folder") from e
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            # copyfile does not copy permission bits; still drop any execute bit
            # that the umask might have left on, so a script is never executable
            # just because it was imported.
            mode = target.stat().st_mode
            exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if mode & exec_bits:
                try:
                    target.chmod(mode & ~exec_bits)
                except OSError:
                    pass
        os.rename(tmp, dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def import_from_folder(path: Any) -> dict[str, Any]:
    """Validate and import. Returns a JSON-friendly result; does not reindex."""
    plan = plan_import(path)
    dest = apply_import(plan)
    return {
        "ok": True,
        "key": plan.name,
        "name": plan.name,
        "description": plan.description,
        "path": str(dest),
        "files": [rel for _src, rel in plan.files],
        "bytes": plan.total_bytes,
        "warnings": [{"code": c, "message": m} for c, m in plan.warnings],
    }
