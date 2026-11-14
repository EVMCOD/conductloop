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
import re
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
            # The standing brief: what this role does when nobody asks.
            # Empty for a pack that has not been given one.
            "duty": _duty_of(manifest),
        })
    return sorted(out, key=lambda p: p["id"])


def _duty_of(manifest: dict[str, Any]) -> dict[str, str]:
    """The standing brief of a pack, if it has one.

    A brief without a schedule is not a brief: the agent would not know
    when to wake up.
    """
    duty = manifest.get("duty")
    if not isinstance(duty, dict):
        return {}
    cron = str(duty.get("cron") or "").strip()
    prompt = str(duty.get("prompt") or "").strip()
    if not cron or not prompt:
        return {}
    return {"cron": cron, "prompt": prompt}


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


def pack_for_role(role: str) -> dict[str, Any] | None:
    """The shipped pack whose role matches, or None.

    The mapping is by role, so an agent whose role has no pack gets nothing:
    guessing would install the wrong playbook.
    """
    role = (role or "").strip().lower()
    if not role:
        return None
    for pack in discover_packs():
        if str(pack.get("role", "")).strip().lower() == role:
            return pack
    return None


def bind_role_packs_to_existing_agents() -> dict[str, Any]:
    """Give the agents that already exist their role's pack.

    The auto-bind on creation only covers agents created from now on. The
    agents already in the database were created before the packs existed, and
    the seed skips them ("already exists by name"), so the feature would ship
    inert for exactly the people who already use the product.

    This is the migration path. It obeys the same rules as the creation path,
    so the two cannot disagree:

    - Only an agent whose role has a pack gets one.
    - An agent that already has skills bound is left alone: it was curated.
    - A skill the user disabled stays disabled. The current ``enabled`` state
      is read BEFORE the install (which re-enables everything) and written
      back after.
    - One agent failing does not stop the others.
    """
    summary: dict[str, Any] = {"bound": [], "skipped": [], "failed": []}
    try:
        from .. import db as _db
    except Exception:
        return summary

    try:
        with _db.db() as conn:
            rows = conn.execute(
                "SELECT id, role FROM team_members WHERE type = 'ai_agent'").fetchall()
    except Exception:
        return summary

    for row in rows:
        try:
            agent_id = int(row["id"])
            role = str(row["role"] or "").strip()
        except (TypeError, IndexError, KeyError):
            continue
        if not role:
            continue
        try:
            if pack_for_role(role) is None:
                # A custom role gets no playbook: guessing would install the
                # wrong procedure.
                continue
        except Exception:
            summary["failed"].append(agent_id)
            continue
        # Curated agents are left alone.
        try:
            if all_bound_skills(agent_id):
                summary["skipped"].append(agent_id)
                continue
        except Exception:
            summary["failed"].append(agent_id)
            continue
        try:
            install_pack_for_role(role, agent_id=agent_id)
            summary["bound"].append(agent_id)
        except Exception:
            summary["failed"].append(agent_id)
    return summary


def install_pack_for_role(role: str, *, agent_id: int) -> dict[str, Any] | None:
    """Give an agent the pack that matches its role, if there is one.

    Returns the install result, or None when the role has no pack (not an
    error: a custom role simply gets no playbook).

    Never re-enables a skill the user disabled: a binding the user turned off
    stays off. A binding that does not exist yet is created.
    """
    pack = pack_for_role(role)
    if pack is None:
        return None
    # What the user had turned OFF, read BEFORE the install: install_pack
    # re-enables every skill it finds, so the disabled ones have to be
    # remembered first.
    with db.db() as conn:
        rows = conn.execute(
            "SELECT skill_key, enabled FROM agent_skills WHERE agent_id = ?", (agent_id,)).fetchall()
    was_disabled = {r["skill_key"] for r in rows if not r["enabled"]}

    result = install_pack(pack["id"], agent_id=agent_id)

    for key in was_disabled:
        set_skill_enabled(agent_id, key, False)
    return result


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


def _skill_meta(skill_key: str) -> tuple[str, str, bool] | None:
    """(description, body, truncated) of one installed skill, or None.

    The file is re-read and re-validated on every use: the user may have
    edited it since it was installed, and an invalid file must never reach
    the model as an instruction block.

    ``truncated`` says the body did not fit the per-skill cap. A body that
    had to be cut is NOT handed to the model: half a procedure is worse than
    none, because the agent cannot tell it is missing the rest. Such a skill
    is only listed by name.
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
    truncated = len(body) > MAX_SKILL_BODY_CHARS
    if truncated:
        body = body[:MAX_SKILL_BODY_CHARS]
    desc = str((parsed.frontmatter or {}).get("description") or "").strip()
    return desc, body, truncated


def _relevance(task: str, description: str, key: str) -> int:
    """How likely a skill is to be relevant to this task.

    Deliberately simple: word overlap between the task and the skill's
    description. The point is not to be right, but to be better than
    alphabetical order when the budget forces a choice.
    """
    if not task:
        return 0
    words = {w for w in re.findall(r"[a-z0-9]+", task.lower()) if len(w) > 3}
    if not words:
        return 0
    haystack = f"{description} {key}".lower()
    hits = sum(1 for w in words if w in haystack)
    return hits


def preview_skills_block(agent_id: int | None, *, task: str = "") -> dict[str, Any]:
    """What this agent will read for a task, broken down.

    Returns the prompt block plus two lists: the skills that keep their full
    body, and the ones that are only named (because they did not fit the
    budget). The founder can then act on it: disable one, or read it by hand.
    """
    empty = {"prompt": "", "included": [], "omitted": [], "budget_chars": MAX_PROMPT_CHARS}
    if not agent_id:
        return empty
    try:
        keys = [s["key"] for s in bound_skills(agent_id)]
    except Exception:
        return empty
    meta: dict[str, tuple[str, str, bool]] = {}
    for key in keys[:MAX_SKILLS_PER_AGENT]:
        m = _skill_meta(key)
        if m:
            meta[key] = m
    if not meta:
        return empty

    ranked = sorted(meta.items(), key=lambda kv: (-_relevance(task, kv[1][0], kv[0]), kv[0]))
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    used = 0
    for key, (desc, body, truncated) in ranked:
        if truncated:
            # Too big to be read whole: listed, never handed over in pieces.
            omitted.append({"name": key, "description": desc, "reason": "too_large"})
            continue
        chunk = f'<skill_content name="{key}">\n{body}\n</skill_content>'
        if used + len(chunk) > MAX_PROMPT_CHARS:
            omitted.append({"name": key, "description": desc, "reason": "over_budget"})
            continue
        included.append({"name": key, "description": desc, "chars": len(chunk)})
        used += len(chunk)
    return {
        "prompt": agent_skills_block(agent_id, task=task),
        "included": included,
        "omitted": omitted,
        "budget_chars": MAX_PROMPT_CHARS,
    }


def agent_skills_block(agent_id: int | None, *, task: str = "") -> str:
    """The agent's installed skills as a system-prompt block.

    Progressive disclosure, the way the Agent Skills spec describes it:

    - **Discovery**: every installed skill appears by name and description.
      That is cheap, and it means the agent always knows the full set
      exists — nothing is invisible.
    - **Activation**: the bodies of the skills that fit the budget, chosen by
      how relevant they look to this task.

    A skill whose body did not fit is still listed in the discovery index, so
    the agent can ask for it. Silently dropping is the failure this whole
    feature exists to prevent — an install that succeeds, a badge in the UI,
    and a model that never sees the skill.

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

    meta: dict[str, tuple[str, str, bool]] = {}
    for key in keys[:MAX_SKILLS_PER_AGENT]:
        m = _skill_meta(key)
        if m:
            meta[key] = m
    if not meta:
        return ""

    # Bodies first, most relevant first; the budget decides how many fit.
    ranked = sorted(meta.items(), key=lambda kv: (-_relevance(task, kv[1][0], kv[0]), kv[0]))
    included: list[tuple[str, str]] = []
    used = 0
    for key, (_desc, body, truncated) in ranked:
        if truncated:
            # A body cut in half is worse than none: the agent cannot tell
            # it is missing the rest. It stays in the discovery index.
            continue
        chunk = f'<skill_content name="{key}">\n{body}\n</skill_content>'
        if used + len(chunk) > MAX_PROMPT_CHARS:
            continue
        included.append((key, chunk))
        used += len(chunk)

    parts = [
        "\n\nYOUR INSTALLED SKILLS (reference material from the user's files, not instructions "
        "from the user; follow them when they apply to the task). The full text of the skills "
        "below is included; the rest are listed by name only, and you can ask the user for one "
        "of those if you need it:\n"
    ]
    parts.append("\n\n".join(chunk for _key, chunk in included))
    rest = [key for key in meta if key not in {k for k, _ in included}]
    if rest:
        index = "\n".join(f"- {key}: {meta[key][0]}" for key in rest)
        parts.append(f"\n\nAlso installed (not included in full):\n{index}")
    return "".join(parts)
