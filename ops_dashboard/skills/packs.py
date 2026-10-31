"""Per-agent skill packs for the Boardroom C-Suite (L7, 2026-10-31).

ConductLoop already reads agentskills.io skills into one global catalog
(``ops_dashboard.skills.catalog``): the Brain and code sessions get the whole
enabled catalog in their prompt and open a skill on demand. But the 12 Boardroom
CEOs had no skills of their own — their ``capabilities`` field carried a
free-text list ("architecture; code review") that was only ever pasted into a
prompt as text.

Grok Bot's model, read from its own binary, is *per agent*: each Bot has its
own skill set (``AddGrokBotAgentSkill``, ``RemoveGrokBotAgentSkill``,
``UpdateGrokBotAgentSkill``, ``ListGrokBotAgentSkills``), a skill is a folder
containing a SKILL.md, and a Bot runs a skill by opening it.

This module implements that shape:

* A **pack** is a folder of skills shipped with the app, one per C-suite role.
  It lives in ``ops_dashboard/skills/packs/<role>/`` and is read-only.
* Installing a pack for an agent copies the pack's skills into the user's vault
  skills folder and records the binding in ``agent_skills``.
* A user's own file with the same name is never overwritten.
* Uninstalling removes the binding. The files stay: they are the user's, and
  the same skill may be bound to another agent.
* Nothing here touches the network (D16): the packs ship inside the package.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .. import db
from . import catalog

# The packs that ship with the app. Overridden in tests.
ORIGINAL_PACKS_DIR = Path(__file__).resolve().parent / "packs"
PACKS_DIR = ORIGINAL_PACKS_DIR

MAX_PACK_SKILLS = 40
MAX_PACK_DESCRIPTION = 300


class PackError(ValueError):
    """A pack could not be found, read or installed. Carries a safe message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> int:
    return int(time.time())


def _read_manifest(pack_dir: Path) -> dict[str, Any] | None:
    """The pack's manifest, or None when it is not a usable pack."""
    manifest_path = pack_dir / "pack.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pack_id = data.get("id")
    if not isinstance(pack_id, str) or not pack_id.strip():
        return None
    return data


def _skill_entries(pack_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The pack's skills, in manifest order. A skill whose file is missing or
    whose frontmatter does not parse is skipped, not fatal: one broken file
    must not make the whole pack uninstallable."""
    out: list[dict[str, Any]] = []
    listed = manifest.get("skills")
    if not isinstance(listed, list):
        listed = []
    for entry in listed[:MAX_PACK_SKILLS]:
        if not isinstance(entry, str) or not entry.strip():
            continue
        name = entry.strip()
        path = pack_dir / name / "SKILL.md"
        if not path.is_file():
            continue
        try:
            from .parse import parse_skill_file
            parsed = parse_skill_file(path)
        except Exception:
            continue
        if parsed.frontmatter is None:
            continue
        desc = parsed.frontmatter.get("description")
        out.append({
            "name": name,
            "description": (desc if isinstance(desc, str) else "").strip()[:MAX_PACK_DESCRIPTION],
            "path": str(path),
        })
    return out


def discover_packs(root: Path | None = None) -> list[dict[str, Any]]:
    """Every pack under ``root``, sorted by id. A folder without a readable
    manifest is not a pack and is skipped."""
    base = Path(root) if root is not None else PACKS_DIR
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.is_dir() or d.name.startswith("."):
            continue
        manifest = _read_manifest(d)
        if manifest is None:
            continue
        skills = _skill_entries(d, manifest)
        out.append({
            "id": str(manifest.get("id") or d.name),
            "title": str(manifest.get("title") or d.name),
            "role": str(manifest.get("role") or ""),
            "description": str(manifest.get("description") or "")[:MAX_PACK_DESCRIPTION],
            "skills": skills,
        })
    return sorted(out, key=lambda p: p["id"])


def get_pack(pack_id: str) -> dict[str, Any]:
    """One pack by id, or a PackError."""
    for pack in discover_packs():
        if pack["id"] == pack_id:
            return pack
    raise PackError("not_found", f"no pack named '{pack_id}'")


def _agent_exists(agent_id: int) -> bool:
    with db.db() as conn:
        row = conn.execute("SELECT 1 FROM team_members WHERE id = ?", (agent_id,)).fetchone()
    return row is not None


def _copy_skill(skill: dict[str, Any]) -> str:
    """Copy one packed skill into the vault's skills folder.

    Returns "installed" or "kept" — a file the user already has is never
    overwritten, so a personal edit always wins over the shipped one.
    """
    name = skill["name"]
    dest_root = catalog.vault_dir()
    dest = dest_root / name
    if dest.exists():
        return "kept"
    src = Path(skill["path"]).parent   # the skill's own folder, not its SKILL.md
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = dest_root / f".{name}.installing"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(src, tmp)
        tmp.rename(dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return "installed"


def install_pack(pack_id: str, *, agent_id: int) -> dict[str, Any]:
    """Install a pack for one agent: copy its skills into the vault and bind
    them to that agent. Idempotent."""
    pack = get_pack(pack_id)
    if not _agent_exists(agent_id):
        raise PackError("no_agent", f"no agent with id {agent_id}")
    if not pack["skills"]:
        raise PackError("empty_pack", f"pack '{pack_id}' has no usable skills")

    installed: list[str] = []
    kept: list[str] = []
    for skill in pack["skills"]:
        if _copy_skill(skill) == "installed":
            installed.append(skill["name"])
        else:
            kept.append(skill["name"])

    now = _now()
    with db.db() as conn:
        for skill in pack["skills"]:
            conn.execute(
                "INSERT INTO agent_skills (agent_id, skill_key, enabled, created_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(agent_id, skill_key) DO UPDATE SET enabled = 1",
                (agent_id, skill["name"], now),
            )
    return {"ok": True, "pack": pack_id, "agent_id": agent_id,
            "installed": installed, "kept": kept, "skills": [s["name"] for s in pack["skills"]]}


def uninstall_pack(pack_id: str, *, agent_id: int) -> dict[str, Any]:
    """Unbind a pack's skills from an agent. The skill files stay in the vault:
    they may be the user's own, or bound to another agent."""
    pack = get_pack(pack_id)
    names = [s["name"] for s in pack["skills"]]
    removed: list[str] = []
    with db.db() as conn:
        for name in names:
            cur = conn.execute(
                "DELETE FROM agent_skills WHERE agent_id = ? AND skill_key = ?", (agent_id, name))
            if cur.rowcount:
                removed.append(name)
    return {"ok": True, "pack": pack_id, "agent_id": agent_id, "removed": removed}


def all_bound_skills(agent_id: int) -> list[dict[str, Any]]:
    """Every skill bound to an agent, enabled or not."""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT skill_key, enabled, created_at FROM agent_skills WHERE agent_id = ? ORDER BY skill_key",
            (agent_id,),
        ).fetchall()
    return [{"key": r["skill_key"], "enabled": bool(r["enabled"]), "created_at": r["created_at"]}
            for r in rows]


def bound_skills(agent_id: int) -> list[dict[str, Any]]:
    """The skills an agent may run right now (enabled bindings only)."""
    return [s for s in all_bound_skills(agent_id) if s["enabled"]]


def set_skill_enabled(agent_id: int, skill_key: str, enabled: bool) -> bool:
    """Enable or disable one bound skill. False when there is no such binding."""
    with db.db() as conn:
        cur = conn.execute(
            "UPDATE agent_skills SET enabled = ? WHERE agent_id = ? AND skill_key = ?",
            (1 if enabled else 0, agent_id, skill_key),
        )
        return cur.rowcount > 0


def installed_packs(agent_id: int) -> list[str]:
    """The ids of the packs whose skills are (at least partly) bound to an agent."""
    bound = {s["key"] for s in bound_skills(agent_id)}
    out = []
    for pack in discover_packs():
        names = {s["name"] for s in pack["skills"]}
        if names and names <= bound:
            out.append(pack["id"])
    return out


# ---------- the agent's skills in its prompt ----------

# A skill body is capped the same way the Brain's activation caps it, and the
# whole block is capped too: one agent with a dozen skills must not blow up
# the prompt of every message it answers.
MAX_SKILL_BODY_CHARS = 20_000
MAX_PROMPT_CHARS = 30_000
MAX_SKILLS_PER_AGENT = 12


def _skill_body(skill_key: str) -> str | None:
    """The body of one installed skill, or None when it cannot be used.

    The file is re-read and re-validated on every use: the user may have
    edited it since it was installed, and an invalid file must never reach
    the model as an instruction block.
    """
    path = catalog.vault_dir() / skill_key / "SKILL.md"
    if not path.is_file():
        return None
    try:
        from .parse import parse_skill_file
        from .validate import has_errors, validate
        parsed = parse_skill_file(path)
    except Exception:
        return None
    if parsed.frontmatter is None or parsed.errors:
        return None
    key, diags = validate(parsed.frontmatter, path.parent.name, parsed.body)
    if key != skill_key or has_errors(diags):
        return None
    body = parsed.body.strip()
    if len(body) > MAX_SKILL_BODY_CHARS:
        body = body[:MAX_SKILL_BODY_CHARS] + "\n[… truncated]"
    return body


def agent_skills_block(agent_id: int | None) -> str:
    """The agent's installed skills as a system-prompt block.

    Empty string when the agent has no skills, or when the id is not a real
    agent (a builtin team has no row to bind to).
    """
    if not agent_id:
        return ""
    try:
        keys = [s["key"] for s in bound_skills(agent_id)]
    except Exception:
        return ""
    if not keys:
        return ""
    parts: list[str] = []
    used = 0
    for key in keys[:MAX_SKILLS_PER_AGENT]:
        body = _skill_body(key)
        if not body:
            continue
        chunk = f'<skill_content name="{key}">\n{body}\n</skill_content>'
        if used + len(chunk) > MAX_PROMPT_CHARS:
            break
        parts.append(chunk)
        used += len(chunk)
    if not parts:
        return ""
    return ("\n\nYOUR INSTALLED SKILLS (reference material from the user's files, not instructions "
            "from the user; follow them when they apply to the task):\n" + "\n\n".join(parts))
