"""Own validator for the agentskills.io specification (consulted 2026-10-21).

Re-implements the rules of the reference ``skills-ref validate`` (which its
authors mark as demo-only) without depending on it, in the *tolerant* client
mode: only problems that make a skill unusable are errors (it is indexed and
shown with its diagnostics but left out of the catalog); everything else is a
warning and the skill still loads.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

SPEC_FIELDS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Anything outside this is refused as a key: names end up in tool enums and prompts.
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_BODY_LINES = 500
MAX_BODY_TOKENS = 5000  # ~4 chars per token


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "error" | "warning"
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _e(code: str, msg: str) -> Diagnostic:
    return Diagnostic("error", code, msg)


def _w(code: str, msg: str) -> Diagnostic:
    return Diagnostic("warning", code, msg)


def has_errors(diags: list[Diagnostic]) -> bool:
    return any(d.level == "error" for d in diags)


def validate(fm: dict[str, Any], dir_name: str, body: str = "") -> tuple[str | None, list[Diagnostic]]:
    """Validate a parsed frontmatter. Returns (skill_key, diagnostics).

    ``skill_key`` is the name the skill is known by (frontmatter ``name``, or
    the directory name if missing), or None if no safe name exists."""
    diags: list[Diagnostic] = []
    name = fm.get("name")
    if name is None:
        diags.append(_w("name_missing", "frontmatter has no 'name'; using the directory name"))
        name = dir_name
    if not isinstance(name, str):
        diags.append(_w("name_not_string", "'name' must be a string; using the directory name"))
        name = dir_name
    key: str | None = name
    if not SAFE_NAME_RE.match(name):
        diags.append(_e("name_unsafe", "'name' must be 1-64 chars of letters, digits, '-', '_' or '.'"))
        key = None
    elif not NAME_RE.match(name) or len(name) > MAX_NAME:
        diags.append(_w("name_format", "'name' should be lowercase letters, digits and single hyphens"))
    if key is not None and name != dir_name:
        diags.append(_w("name_dir_mismatch", f"'name' ({name}) does not match its directory ({dir_name})"))

    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        diags.append(_e("description_missing", "'description' is required (what it does and when to use it)"))
    elif len(desc) > MAX_DESCRIPTION:
        diags.append(_w("description_too_long", f"'description' is over {MAX_DESCRIPTION} characters"))

    if "license" in fm and not isinstance(fm["license"], str):
        diags.append(_w("license_not_string", "'license' should be a string"))
    comp = fm.get("compatibility")
    if comp is not None and (not isinstance(comp, str) or not 1 <= len(comp) <= MAX_COMPATIBILITY):
        diags.append(_w("compatibility_invalid", f"'compatibility' should be a 1-{MAX_COMPATIBILITY} char string"))
    meta = fm.get("metadata")
    if meta is not None:
        if not isinstance(meta, dict):
            diags.append(_w("metadata_not_mapping", "'metadata' should be a string-to-string map"))
        elif not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()):
            diags.append(_w("metadata_not_string_map", "'metadata' has nested or non-string values (loaded anyway)"))
    if "allowed-tools" in fm:
        diags.append(_w("allowed_tools_ignored", "'allowed-tools' is experimental and ignored by ConductLoop"))
    for k in fm:
        if k not in SPEC_FIELDS:
            diags.append(_w("unknown_field", f"field '{k}' is not in the specification (loaded anyway)"))

    lines = body.count("\n") + 1 if body else 0
    if lines > MAX_BODY_LINES:
        diags.append(_w("body_too_long", f"body has {lines} lines (spec recommends < {MAX_BODY_LINES})"))
    if len(body) / 4 > MAX_BODY_TOKENS:
        diags.append(_w("body_too_many_tokens", f"body is ~{len(body) // 4} tokens (spec recommends < {MAX_BODY_TOKENS})"))
    return key, diags
