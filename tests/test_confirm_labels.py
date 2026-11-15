"""The dialog's buttons speak the language of the page.

``confirm.js`` keeps English as its default: the product's default copy is
English, and a page that ships another language passes its own words
(``opts.confirm`` / ``opts.cancel`` / ``opts.ok``). The dashboard did not
pass them, so every question it asked answered with "Confirm", "Cancel" and
"OK" inside a Spanish app (2026-11-15, prompt-minimax-2026-11-15 P1-2).

These tests are the guard that keeps the pattern from coming back: every
call passes its labels, the labels come from the page's own dictionary, and
the dictionary has them in every language it ships.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
HELPER = WEB / "confirm.js"

# The pages that can ask. confirm.js itself defines the helpers.
ASKERS = sorted(p for p in WEB.glob("*.js") if p.name != "confirm.js")

# A call with its argument list: confirmAction(...) / promptAction(...).
# The argument list may span lines, so the closing parenthesis is matched by
# counting, not with a regex.
_CALL = re.compile(r"(?<![\w.$])(confirmAction|promptAction)\s*\(")


def _calls(text: str) -> list[tuple[str, str]]:
    """Every (name, argument list) of a confirm/prompt call in the text."""
    out = []
    for m in _CALL.finditer(text):
        depth = 0
        for i in range(m.end() - 1, len(text)):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    out.append((m.group(1), text[m.end():i]))
                    break
    return out


def _code(text: str) -> str:
    """The code, without the comments.

    A mention in a comment is not a question: the dashboard's file explains
    that "New agent" used to ask three questions through promptAction().
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        # A "//" that is not the tail of "://" (a URL) starts a comment.
        lines.append(re.sub(r"(?<!:)//.*$", "", line))
    return "\n".join(lines)


def _dictionaries() -> dict[str, dict]:
    out = {}
    for path in WEB.glob("i18n/*.json"):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


def test_every_question_passes_its_own_labels():
    """No call may fall back to the English defaults by accident."""
    missing = []
    for path in ASKERS:
        for name, args in _calls(_code(path.read_text(encoding="utf-8"))):
            # The labels are the second argument: after the message, after
            # the message and its comma.
            rest = args.split(",", 1)
            if len(rest) < 2:
                missing.append(f"{path.name}: {name}({args.strip()})")
                continue
            labels = rest[1]
            if not re.search(r"\b(confirm|cancel|ok)\s*:", labels) and not re.search(r"\b_confirmLabels\(|\b_promptLabels\(", labels):
                missing.append(f"{path.name}: {name}({args.strip()})")
    assert not missing, "these questions would answer in English:\n" + "\n".join(missing)


def test_the_dashboard_reads_its_labels_from_its_own_dictionary():
    text = (WEB / "app.js").read_text(encoding="utf-8")
    for key in ("dashboard.dlg_confirm", "dashboard.dlg_cancel", "dashboard.dlg_ok"):
        assert key in text, key
    # And the helper builds them out of the page's dictionary, not out of a
    # hard-coded English word.
    assert re.search(r"function _confirmLabels\(\)\s*\{[^}]*tr\('dashboard\.dlg_confirm'\)", text)
    assert re.search(r"function _promptLabels\([^)]*\)\s*\{[^}]*tr\('dashboard\.dlg_ok'\)", text)


def test_the_labels_are_in_the_dashboard_dictionary_in_every_language():
    dicts = _dictionaries()
    assert "en" in dicts and "es" in dicts
    for code in ("en", "es"):
        dash = dicts[code].get("dashboard", {})
        for key in ("dlg_confirm", "dlg_cancel", "dlg_ok"):
            assert dash.get(key), f"{code}: dashboard.{key} is missing"
    # The Spanish words are Spanish: an English string left in the Spanish
    # dictionary is the bug this whole point is about.
    es = dicts["es"]["dashboard"]
    assert es["dlg_confirm"] != dicts["en"]["dashboard"]["dlg_confirm"]
    assert es["dlg_cancel"] != dicts["en"]["dashboard"]["dlg_cancel"]


def test_the_helper_still_ships_english_as_its_default():
    """The product's default copy is English; a page passes its own words."""
    text = HELPER.read_text(encoding="utf-8")
    assert "DEFAULT_LABELS" in text
    assert re.search(r"DEFAULT_LABELS\s*=\s*\{[^}]*confirm:\s*'Confirm'", text)
    assert re.search(r"DEFAULT_LABELS\s*=\s*\{[^}]*cancel:\s*'Cancel'", text)
    # The labels come from the options when they are there.
    assert "function word(opts, key)" in text
