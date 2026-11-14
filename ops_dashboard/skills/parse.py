"""SKILL.md parsing: YAML frontmatter (PyYAML ``safe_load``, D8) + Markdown body.

Tolerant, as the agentskills.io client guide recommends: if the YAML does not
parse, retry once quoting top-level ``key: value`` lines whose value contains
``: ``; if it still fails the skill is reported with an error diagnostic and
skipped from the catalog. YAML aliases/anchors are refused (no "billion
laughs"-style expansion) and sizes are capped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MAX_SKILL_BYTES = 256 * 1024
MAX_FRONTMATTER_BYTES = 32 * 1024

_TOP_KV = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]+(.+?)[ \t]*$")


@dataclass
class ParsedSkill:
    frontmatter: dict[str, Any] | None
    body: str
    errors: list[tuple[str, str]] = field(default_factory=list)    # (code, message)
    warnings: list[tuple[str, str]] = field(default_factory=list)


def _split(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body). Frontmatter must open on line 1."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).lstrip("\n")
    return None, text


def _has_alias_or_anchor(fm: str) -> bool:
    try:
        for tok in yaml.scan(fm, Loader=yaml.SafeLoader):
            if isinstance(tok, (yaml.AliasToken, yaml.AnchorToken)):
                return True
    except yaml.YAMLError:
        return False  # the parse step reports it
    return False


def _quote_colon_values(fm: str) -> str:
    out = []
    for line in fm.split("\n"):
        m = _TOP_KV.match(line)
        if m and ": " in m.group(2) and m.group(2)[0] not in "\"'|>[{":
            v = m.group(2).replace("\\", "\\\\").replace('"', '\\"')
            line = f'{m.group(1)}: "{v}"'
        out.append(line)
    return "\n".join(out)


def _load(fm: str) -> Any:
    return yaml.safe_load(fm)


def parse_skill_text(text: str) -> ParsedSkill:
    fm_text, body = _split(text)
    if fm_text is None:
        return ParsedSkill(None, body, errors=[("no_frontmatter", "SKILL.md must start with a '---' YAML frontmatter block")])
    if len(fm_text.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        return ParsedSkill(None, body, errors=[("frontmatter_too_large", f"frontmatter > {MAX_FRONTMATTER_BYTES} bytes")])
    if _has_alias_or_anchor(fm_text):
        return ParsedSkill(None, body, errors=[("yaml_alias_not_allowed", "YAML anchors/aliases are not allowed")])
    warnings: list[tuple[str, str]] = []
    try:
        data = _load(fm_text)
    except yaml.YAMLError:
        try:
            data = _load(_quote_colon_values(fm_text))
            warnings.append(("yaml_quoted_retry", "frontmatter only parsed after quoting values that contain ': '"))
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (line {mark.line + 2})" if mark is not None else ""
            return ParsedSkill(None, body, errors=[("yaml_unparseable", f"frontmatter is not valid YAML{where}")])
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return ParsedSkill(None, body, errors=[("frontmatter_not_mapping", "frontmatter must be a YAML mapping")])
    return ParsedSkill({str(k): v for k, v in data.items()}, body, warnings=warnings)


def parse_skill_file(path: Path) -> ParsedSkill:
    try:
        size = path.stat().st_size
    except OSError:
        return ParsedSkill(None, "", errors=[("unreadable", "SKILL.md cannot be read")])
    if size > MAX_SKILL_BYTES:
        return ParsedSkill(None, "", errors=[("too_large", f"SKILL.md > {MAX_SKILL_BYTES} bytes")])
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ParsedSkill(None, "", errors=[("unreadable", "SKILL.md cannot be read")])
    return parse_skill_text(text)
