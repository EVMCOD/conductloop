"""The conventions ConductLoop assumes about your vault, in one place.

The vault features (handoffs, open work, the waiting page, the daily note, the
index sections) read your notes through a few conventions: which notes are
handoffs, who the owner at the end of an open item is, what language the
generated headings are in. They used to be fixed in the code, and some of them
were one person's habits. They now come from the ``vault_conventions`` object
of ``config.json`` (``$OPS_CONFIG_PATH``); every key is optional and the
defaults are neutral.

.. code-block:: json

    {
      "vault_conventions": {
        "language": "en",
        "handoff_types": ["handoff"],
        "handoff_folders": ["handoff", "handoffs"],
        "ask_headings": [],
        "owners": [],
        "blocking_headings": [],
        "daily_dir": "",
        "unplaced_section": "📥 Sin clasificar",
        "orphan_section": "📚 Documentación",
        "ideas_section": "💡 Ideas",
        "counts_template": ""
      }
    }

See docs/VAULT-CONVENTIONS.md for what each key does. A value of the wrong
type is ignored (with a warning) and the default is used: a typo in
``config.json`` must never stop the app from indexing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import string
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import app_paths, db

log = logging.getLogger("conductloop.vault_conventions")

LANGUAGES = ("en", "es")

# The index sections are *written into the user's own index* and found again
# by their title. Changing these defaults would make the product add a second
# "Unsorted" section next to the "Sin clasificar" an existing index already
# has, so they stay as they were; change them in the config for a new vault.
DEFAULT_UNPLACED_SECTION = "📥 Sin clasificar"
DEFAULT_ORPHAN_SECTION = "📚 Documentación"
DEFAULT_IDEAS_SECTION = "💡 Ideas"
# "42 proyectos (33 notas activas + 9 ideas)": the counts line of an index.
DEFAULT_COUNTS_TEMPLATE = "{total} proyectos ({active} notas activas + {ideas} ideas)"
DEFAULT_COUNTS_RE = re.compile(
    r"\d+\s+proyectos\s*\(\s*\d+\s+notas activas\s*\+\s*\d+\s+ideas\s*\)")

# Owners an open item can end with ("- [ ] do X — Claude"), besides the
# owner's own name ($OPS_OWNER_NAME) and the ones in the config.
DEFAULT_OWNERS = ("Claude", "Codex", "Cursor", "ChatGPT")

_COUNTS_FIELDS = ("total", "active", "ideas")


@dataclass(frozen=True)
class Conventions:
    language: str = "en"
    handoff_types: tuple[str, ...] = ("handoff",)
    handoff_folders: tuple[str, ...] = ("handoff", "handoffs")
    ask_headings: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    blocking_headings: tuple[str, ...] = ()
    daily_dir: str = ""
    unplaced_section: str = DEFAULT_UNPLACED_SECTION
    orphan_section: str = DEFAULT_ORPHAN_SECTION
    ideas_section: str = DEFAULT_IDEAS_SECTION
    counts_template: str = ""

    # ── derived values ────────────────────────────────────────────────

    def all_owners(self) -> tuple[str, ...]:
        """Every name an open item may end with, without duplicates."""
        names: list[str] = []
        me = os.environ.get("OPS_OWNER_NAME", "").strip()
        if me and me.lower() not in ("the operator", "the user"):
            names.append(me)
        names.extend(self.owners)
        names.extend(DEFAULT_OWNERS)
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
        return tuple(out)

    def owner_re(self) -> re.Pattern[str]:
        """``... — Name`` at the end of a line."""
        alternatives = "|".join(re.escape(n) for n in self.all_owners())
        return re.compile(rf"\s*[—–-]\s*({alternatives})\s*$")

    def blocking_re(self, base: str) -> BlockingMatcher:
        """The built-in blocking-heading pattern plus the configured ones.

        Every pattern is compiled on its own, so a configured regex keeps its
        own leading inline flags (``(?i)blocked``) and its own groups; one that
        does not compile is skipped (with a warning) and never breaks a scan.
        """
        compiled = [re.compile(base, re.IGNORECASE)]
        for pattern in self.blocking_headings:
            one = _compile_blocking(pattern)
            if one is not None:
                compiled.append(one)
        return BlockingMatcher(tuple(compiled))

    def counts_re(self) -> re.Pattern[str]:
        return _template_to_re(self.counts_template) if self.counts_template else DEFAULT_COUNTS_RE

    def counts_line(self, *, total: int, active: int, ideas: int) -> str:
        template = self.counts_template or DEFAULT_COUNTS_TEMPLATE
        return template.format(total=total, active=active, ideas=ideas)


@lru_cache(maxsize=256)
def _compile_blocking(pattern: str) -> re.Pattern[str] | None:
    """One configured blocking pattern, case-insensitive; None when invalid."""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except (re.error, RecursionError, OverflowError) as exc:
        log.warning("vault_conventions: ignoring the invalid blocking pattern %r (%s)", pattern, exc)
        return None


class BlockingMatcher:
    """Several compiled patterns that behave like one for ``.search``."""

    def __init__(self, patterns: tuple[re.Pattern[str], ...]) -> None:
        self.patterns = patterns

    def search(self, text: str) -> re.Match[str] | None:
        for pattern in self.patterns:
            found = pattern.search(text)
            if found:
                return found
        return None


# What ``str.format`` may do with a counts placeholder. The counts are plain
# integers, so the placeholder grammar is the integer one:
#   {total}  {active:03d}  {ideas:,}  {total:>4}  {active!s}
# ``[[fill]align][sign][0][width][,|_][d]``, or ``[[fill]align][width]`` after a
# ``!s``/``!r``/``!a`` conversion (which turns the number into text first).
_INT_SPEC = re.compile(
    r"(?:(?P<fill>.)?(?P<align>[<>=^]))?(?P<sign>[+\- ])?(?P<zero>0)?"
    r"(?P<width>\d+)?(?P<group>[,_])?(?P<kind>d)?", re.DOTALL)


def _field_re(spec: str, conversion: str | None) -> str | None:
    """Regex for one rendered placeholder, None if the spec is not supported."""
    m = _INT_SPEC.fullmatch(spec)
    if not m:
        return None
    if conversion and (m["sign"] or m["zero"] or m["group"] or m["kind"]):
        return None
    fill = m["fill"] or " "
    # Padding only exists when a width asks for it; without one the placeholder
    # is exactly the number, so the spaces around it stay the literal's.
    pad = ""
    if m["width"]:
        pad = r"\s*" if fill.isspace() else f"(?:{re.escape(fill)})*"
    sign = {"+": r"[+\-]", " ": r"[ \-]?"}.get(m["sign"] or "", r"-?")
    return rf"{pad}{sign}{pad}\d[\d,_]*{pad}"


def _parse_counts_template(template: str) -> list[tuple[str, str | None, str | None, str | None]] | None:
    """``string.Formatter().parse`` of the template; None when it is unusable."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    for _literal, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in _COUNTS_FIELDS or "{" in (spec or "") or conversion not in (None, "s", "r", "a"):
            return None
    return parsed


def _template_to_re(template: str) -> re.Pattern[str]:
    """``"{total} projects ({active:03d} active)"`` → a regex that finds that line."""
    parsed = _parse_counts_template(template) or []
    out: list[str] = []
    for literal, name, spec, conversion in parsed:
        out.append(r"\s+".join(re.escape(w) for w in literal.split(" ")))
        if name is not None:
            out.append(_field_re(spec or "", conversion) or r"\d+")
    return re.compile("".join(out))


def _valid_template(template: str) -> bool:
    """True when the template renders and ``_template_to_re`` finds its own output."""
    parsed = _parse_counts_template(template)
    if not parsed or not any(name for _l, name, _s, _c in parsed):
        return False
    if any(name is not None and _field_re(spec or "", conv) is None for _l, name, spec, conv in parsed):
        return False
    try:
        pattern = _template_to_re(template)
        for sample in ((1, 1, 1), (0, 7, 42), (1234567, 89, 3)):
            line = template.format(total=sample[0], active=sample[1], ideas=sample[2])
            if not pattern.fullmatch(line):
                return False
    except (KeyError, IndexError, ValueError, TypeError):
        return False
    return True


def _strings(raw: Any, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        log.warning("vault_conventions.%s must be a list of strings; using the default", key)
        return default
    return tuple(x.strip() for x in raw if x.strip())


def _string(raw: Any, key: str, default: str) -> str:
    if raw is None:
        return default
    if not isinstance(raw, str):
        log.warning("vault_conventions.%s must be a string; using the default", key)
        return default
    return raw.strip() or default


def parse(raw: Any) -> Conventions:
    """Build the conventions from the ``vault_conventions`` object of the config."""
    if not isinstance(raw, dict):
        if raw is not None:
            log.warning("vault_conventions must be an object; using the defaults")
        raw = {}
    language = _string(raw.get("language"), "language", "en").lower()
    if language not in LANGUAGES:
        log.warning("vault_conventions.language %r is not one of %s; using 'en'", language, LANGUAGES)
        language = "en"
    template = _string(raw.get("counts_template"), "counts_template", "")
    if template and not _valid_template(template):
        log.warning("vault_conventions.counts_template must use {total}, {active}, {ideas} "
                    "(optionally with an integer format such as {active:03d}); ignored")
        template = ""
    return Conventions(
        language=language,
        handoff_types=tuple(t.lower() for t in _strings(raw.get("handoff_types"), "handoff_types", ("handoff",))),
        handoff_folders=tuple(t.lower() for t in _strings(raw.get("handoff_folders"), "handoff_folders",
                                                          ("handoff", "handoffs"))),
        ask_headings=tuple(t.lower() for t in _strings(raw.get("ask_headings"), "ask_headings", ())),
        owners=_strings(raw.get("owners"), "owners", ()),
        blocking_headings=_strings(raw.get("blocking_headings"), "blocking_headings", ()),
        daily_dir=_string(raw.get("daily_dir"), "daily_dir", ""),
        unplaced_section=_string(raw.get("unplaced_section"), "unplaced_section", DEFAULT_UNPLACED_SECTION),
        orphan_section=_string(raw.get("orphan_section"), "orphan_section", DEFAULT_ORPHAN_SECTION),
        ideas_section=_string(raw.get("ideas_section"), "ideas_section", DEFAULT_IDEAS_SECTION),
        counts_template=template,
    )


_cache: dict[str, Any] = {"key": None, "value": None}


def get() -> Conventions:
    """The conventions in force. Re-read when ``config.json`` changes."""
    path = app_paths.config_path()
    try:
        stat = path.stat()
        key: Any = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(path), None, None)
    if _cache["key"] == key and _cache["value"] is not None:
        return _cache["value"]  # type: ignore[no-any-return]
    raw: Any = None
    if key[1] is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get("vault_conventions")
        except (OSError, ValueError, AttributeError):
            raw = None
    value = parse(raw)
    env_lang = os.environ.get("OPS_VAULT_LANGUAGE", "").strip().lower()
    if env_lang in LANGUAGES:
        value = replace(value, language=env_lang)
    _cache["key"], _cache["value"] = key, value
    return value


def reset_cache() -> None:
    _cache["key"] = _cache["value"] = None


# ── the daily note ────────────────────────────────────────────────────────

# Where the product wrote the daily note before it was configurable. It is
# still used when it already exists on this machine, so an upgrade does not
# move anybody's daily notes; a new install gets <vault>/daily.
_LEGACY_DAILY = Path.home() / "Documents" / "Obsidian" / "OC" / "daily"


def daily_dir() -> Path:
    """Folder of the daily notes: $OPS_DAILY_DIR, the config, the old
    location when it exists, else ``<vault root>/daily``."""
    explicit = os.environ.get("OPS_DAILY_DIR", "").strip() or get().daily_dir
    if explicit:
        return Path(explicit).expanduser()
    if _LEGACY_DAILY.is_dir():
        return _LEGACY_DAILY
    return db.vault_root() / "daily"


# Texts of the vault half of the daily note.
DAILY_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "## 🗺️ The vault",
        "moved": "**Projects that moved**",
        "notes_n": "{n} note(s)",
        "ideas": "**Recurring topics**",
        "ideas_n": "{n} notes",
        "touched": "**Notes touched**",
        "more": "… and {n} more",
    },
    "es": {
        "title": "## 🗺️ El vault",
        "moved": "**Proyectos que se movieron**",
        "notes_n": "{n} nota(s)",
        "ideas": "**Temas que se repiten**",
        "ideas_n": "{n} notas",
        "touched": "**Notas tocadas**",
        "more": "… y {n} más",
    },
}


def daily_text(key: str, **fmt: Any) -> str:
    table = DAILY_TEXT.get(get().language) or DAILY_TEXT["en"]
    return table[key].format(**fmt)
