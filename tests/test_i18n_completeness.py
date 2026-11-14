"""The panel speaks the language the person chose. This keeps it that way.

Every page of the app gets its words from a dictionary: the shared one
(``web/i18n/<locale>.json``) and, for the pages with their own vocabulary, a
dictionary of their own (``web/i18n/<page>.<locale>.json``). A few pages also
carry a copy of their words inside the file, as a fallback for when the
dictionary has not arrived over the network yet.

Three things can go wrong, and all three show up in the interface as English
in a panel that was supposed to be speaking something else:

1. A word is added to one language and not to the other: the person who
   chose the other language reads it in English.
2. A word is used but never defined: the translator falls back to the key
   itself, so ``skills.dismiss_failed`` appears on the screen.
3. A word is written in the page instead of in the dictionary: it never
   changes language, whatever the person chose.

This file checks the first two for every page, and the third for the shared
panel only (``app.js`` and ``dialog.js``), because that is where the words
of the whole app are built.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
I18N = WEB / "i18n"
LOCALES = ("en", "es")

# The words of the page: ``'<page>.' + key``. The pages that do it this way
# carry a dictionary of their own under the same name.
NAMESPACE_PREFIX = re.compile(r"'([a-z][a-z0-9-]*\.)'\s*\+\s*key")

# A translator call: tr('key'), t('key'). A key built by concatenation
# ('st_' + status) is not one key, so it does not count.
TRANSLATOR_CALL = re.compile(
    r"""(?<![\w.])(?:\btr|\bt)\(\s*(['"])([A-Za-z0-9_.-]+)\1(?!\s*\+)"""
)

# A word asked to the translator of another page: H.t('key'), i18n.t('key').
# It lives in that page's dictionary, not in the copy of the file it is in.
FOREIGN_CALL = re.compile(
    r"""\b\w+\.(?:tr|t)\(\s*(['"])([A-Za-z0-9_.-]+)\1(?!\s*\+)"""
)

# A word marked in the HTML for the translator: data-i18n="key" and the
# welcome page's own data-welcome="key".
MARKED_ATTRIBUTE = re.compile(
    r"""data-i18n(?:-ph|-title|-label)?\s*=\s*(['"])([A-Za-z0-9_.-]+)\1"""
)
WELCOME_ATTRIBUTE = re.compile(
    r"""data-welcome(?:-ph)?\s*=\s*(['"])([A-Za-z0-9_.-]+)\1"""
)

# The copy a page carries inside the file, as a fallback for when the
# dictionary has not arrived: L = { en: {…}, es: {…} } or T = {…}.
INLINE_COPY = re.compile(r"\b[LT]\s*=\s*\{")
INLINE_KEY = re.compile(r"(?:^|[,{])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")

# The shared panel: the words of the whole app are built here.
SHARED_PANEL = ("app.js", "dialog.js")

# Values that are not words: a source kind, a language code, the brand. They
# are the same in every language and must not be translated.
NOT_WORDS = {
    "ConductLoop",
    "mixed",
    "human",
    "ai_squad",
    "ai_agent",
    "system",
    "cron",
    "webhook",
    "event",
    "EN",
    "ES",
}


def flatten(obj, prefix: str = "") -> set[str]:
    """Every leaf of a dictionary, as dotted paths."""
    out: set[str] = set()
    for key, value in obj.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out |= flatten(value, path + ".")
        else:
            out.add(path)
    return out


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dictionary_pairs() -> list[tuple[str, Path, Path]]:
    """(name, english file, spanish file) for the shared pair and each page."""
    pairs = [("(shared)", I18N / "en.json", I18N / "es.json")]
    for en in sorted(I18N.glob("*.en.json")):
        if en.name == "en.json":
            continue
        es = en.with_name(en.name.replace(".en.json", ".es.json"))
        if es.exists():
            pairs.append((en.name[: -len(".en.json")], en, es))
    return pairs


def merged(locale: str) -> dict:
    """The dictionary the browser actually has: the shared one plus the pages."""
    out: dict = {}
    for _, en, _ in dictionary_pairs():
        source = en if locale == "en" else en.with_name(en.name.replace(".en.json", f".{locale}.json"))
        if source.exists():
            for ns, words in load(source).items():
                if isinstance(words, dict):
                    out.setdefault(ns, {}).update(words)
    return out


def used_keys(path: Path) -> tuple[set[str], set[str], set[str]]:
    """The keys the page asks the translator for.

    The first set is namespaced (how the dictionary stores them), the second
    is raw (how a copy written inside the file stores them), the third is the
    words the welcome page marks in its HTML.
    """
    src = path.read_text(encoding="utf-8")
    prefix = NAMESPACE_PREFIX.search(src)
    ns = prefix.group(1) if prefix else ""
    namespaced: set[str] = set()
    raw: set[str] = set()
    welcome: set[str] = set()
    for _, key in TRANSLATOR_CALL.findall(src):
        namespaced.add(ns + key if ns and "." not in key else key)
        raw.add(key)
    for _, key in FOREIGN_CALL.findall(src):
        namespaced.add(key)
    for _, key in MARKED_ATTRIBUTE.findall(src):
        namespaced.add(key)
        raw.add(key)
    for _, key in WELCOME_ATTRIBUTE.findall(src):
        welcome.add(key)
    return namespaced, raw, welcome


def _block(src: str, start: int) -> str:
    """The body of an object literal, from just after the opening brace.

    Braces inside a string do not count: a copy full of words like
    ``'{n} files'`` would otherwise end the block in the wrong place.
    """
    depth, i = 1, start
    quote = ""
    while i < len(src) and depth:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return src[start : i - 1]


def inline_keys(path: Path) -> dict[str, set[str]] | None:
    """The words a page carries inside the file: {'en': {…}, 'es': {…}}."""
    src = path.read_text(encoding="utf-8")
    m = INLINE_COPY.search(src)
    if not m:
        return None
    body = _block(src, m.end())
    out: dict[str, set[str]] = {}
    for locale in LOCALES:
        lm = re.search(rf"\b{locale}\s*:\s*\{{", body)
        if not lm:
            return None
        out[locale] = set(INLINE_KEY.findall(_block(body, lm.end())))
    return out


def resolves(cache: dict, key: str) -> bool:
    cur = cache
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def resolves_anywhere(cache: dict, key: str) -> bool:
    """The word exists, with this name or under the namespace of its page.

    A page can ask for a word of another page (``H.t('dlg_confirm')`` from a
    page loaded inside another one), so the name alone does not say where it
    lives.
    """
    if resolves(cache, key):
        return True
    tail = key.split(".")[-1]
    return any(
        isinstance(words, dict) and tail in words for words in cache.values()
    )


# ── 1. the two languages define the same words ────────────────────────────────


def test_every_dictionary_defines_the_same_words_in_both_languages():
    """A word in one language and not in the other is English in the panel."""
    broken = []
    for name, en, es in dictionary_pairs():
        only_en = flatten(load(en)) - flatten(load(es))
        only_es = flatten(load(es)) - flatten(load(en))
        if only_en or only_es:
            broken.append(
                f"{name}: only in en {sorted(only_en)[:8]}, only in es {sorted(only_es)[:8]}"
            )
    assert not broken, "\n".join(broken)


# ── 2. every word the panel asks for is written in both languages ────────────


def test_every_word_the_panel_uses_is_written_in_both_languages():
    """A word that is not in the dictionary is shown as the key itself."""
    broken = []
    for path in sorted(list(WEB.glob("*.js")) + list(WEB.glob("*.html"))):
        if path.name == "i18n.js":
            continue
        namespaced, _, welcome = used_keys(path)
        if not namespaced and not welcome:
            continue
        copy = inline_keys(path)
        for locale in LOCALES:
            cache = merged(locale)
            for key in sorted(namespaced):
                if resolves_anywhere(cache, key):
                    continue
                # A page with its own copy inside the file does not need the
                # dictionary; the copy is checked for both languages instead.
                if copy and key.split(".")[-1] in copy[locale]:
                    continue
                broken.append(f"{locale}: {key} (used in {path.name})")
    assert not broken, "\n".join(broken)


def test_the_copy_inside_a_page_is_written_in_both_languages():
    """The copy inside the file is what a person reads while the words load.

    A word the page asks for has to be there in both languages: otherwise the
    other one falls back to the first (the welcome page) or to the key itself
    (the rest).

    The welcome page is the exception: its HTML is already in English and it
    only replaces the words when the person chose Spanish, so its English
    copy does not have to be complete.
    """
    broken = []
    for path in sorted(WEB.glob("*.js")):
        copy = inline_keys(path)
        if not copy:
            continue
        _, raw, _ = used_keys(path)
        one_way = path.name == "welcome.js"
        for key in sorted(raw):
            for locale in LOCALES:
                if key in copy[locale]:
                    continue
                if one_way and locale == "en":
                    continue
                broken.append(f"{locale}: {key} (copy inside {path.name})")
    assert not broken, "\n".join(broken)


def test_the_welcome_page_writes_its_words_in_spanish():
    """The welcome page replaces its words when the person chose Spanish."""
    copy = inline_keys(WEB / "welcome.js")
    assert copy is not None, "the welcome page carries its copy inside the file"
    for _, key in WELCOME_ATTRIBUTE.findall(
        (WEB / "welcome.html").read_text(encoding="utf-8")
    ):
        assert key in copy["es"], f"welcome: {key} is not written in Spanish"


# ── 3. the shared panel does not write its own words ─────────────────────────

# The words of the panel are put into the page on these lines. The text is
# usually built by concatenating pieces, and the piece with the words is
# rarely the first one, so every literal of the line is looked at.
HTML_LINE = re.compile(r"html\s*\+=|innerHTML\s*=|textContent\s*=|setAttribute\(")
MESSAGE_CALL = re.compile(r"\b(?:notify|skillFromRunMsg)\(\s*(['\"])([^'\"\n]*)\1")
# A key of the translator always carries the namespace of its page
# ('dashboard.skills_title'). A word without a dot is a word.
KEY_LIKE = re.compile(r"[a-z][a-z0-9_]*\.[a-z0-9_]+")


def literals_in(line: str) -> list[tuple[str, int, int]]:
    """The string literals of a line, with where they start and end.

    A literal can hold the other kind of quote inside it (an HTML attribute
    inside a single-quoted string), so the quotes are counted by hand.
    """
    out: list[tuple[str, int, int]] = []
    i, size = 0, len(line)
    while i < size:
        ch = line[i]
        if ch not in "'\"":
            i += 1
            continue
        quote = ch
        start = i
        i += 1
        buf: list[str] = []
        while i < size:
            c = line[i]
            if c == "\\" and i + 1 < size:
                buf.append(line[i : i + 2])
                i += 2
                continue
            if c == quote:
                break
            buf.append(c)
            i += 1
        out.append(("".join(buf), start, i + 1))
        i += 1
    return out


# What can sit between two literals for them to be one piece of text: the
# plus signs and the spaces. Anything else (a call, a variable) means the
# text in between is written somewhere else.
BETWEEN = re.compile(r"[\s+]*")


def pieces_of_text(line: str) -> list[str]:
    """The pieces of text a line writes, with the pieces next to each other.

    ``'<button>' + 'cancel' + '</button>'`` is one piece of text. ``'<div>' +
    esc(tr('dashboard.x')) + '</div>'`` is three: the words of the middle
    one are written somewhere else.
    """
    groups: list[str] = []
    current: list[str] = []
    previous_end = 0
    for text, start, end in literals_in(line):
        if current and not BETWEEN.fullmatch(line[previous_end:start]):
            groups.append("".join(current))
            current = []
        current.append(text)
        previous_end = end
    if current:
        groups.append("".join(current))
    return groups


def visible_words(literal: str) -> list[str]:
    """The words of a fragment of HTML: what a person reads between the tags."""
    # What is inside <code> is a command to run or a value, not a word: it
    # is the same in every language.
    literal = re.sub(r"<code[^>]*>.*?</code>", " ", literal, flags=re.S)
    # An option whose label is its own value shows a name (the provider
    # 'anthropic'), not a word to translate.
    literal = re.sub(r"<option value=\"([^\"]*)\">\1</option>", " ", literal)
    words = []
    for text in re.findall(r">([^<>]+)<", literal):
        text = text.strip()
        if not re.search(r"[A-Za-z]{2,}", text):
            continue
        # A key of the translator ('dashboard.skills_title') and a piece of
        # code ('/auto', 'var(--red)') are not words a person reads.
        if KEY_LIKE.fullmatch(text):
            continue
        if re.search(r"[/;:{}=()#%]", text):
            continue
        words.append(text)
    if re.search(r"[.!?]\s+[A-Z]", literal):
        words.append(literal.strip())
    return words


def test_the_shared_panel_does_not_write_its_own_words():
    """The words of the whole app are built here, so none of them can be English."""
    broken = []
    for name in SHARED_PANEL:
        src = (WEB / name).read_text(encoding="utf-8")
        for number, line in enumerate(src.splitlines(), start=1):
            if not HTML_LINE.search(line):
                continue
            # The words are usually built by concatenating pieces, and the
            # piece with the words is rarely the first one. Putting the
            # pieces together is what a person ends up reading.
            for piece in pieces_of_text(line):
                for word in visible_words(piece):
                    if word in NOT_WORDS:
                        continue
                    broken.append(f"{name}:{number}: {word!r}")
        for _, literal in MESSAGE_CALL.findall(src):
            if len(re.findall(r"[A-Za-z]{2,}", literal)) >= 2:
                broken.append(f"{name}: {literal!r}")
    assert not broken, "\n".join(broken)


def test_the_dialog_takes_its_words_from_the_dictionary():
    """Every modal in the panel opens with the words of the chosen language."""
    src = (WEB / "dialog.js").read_text(encoding="utf-8")
    for key in ("common.cancel", "common.submit", "common.confirm", "common.optional"):
        assert f"t('{key}'" in src, f"dialog.js does not ask for {key}"
    for locale in LOCALES:
        catalog = load(I18N / f"{locale}.json")
        for key in ("cancel", "submit", "confirm", "optional"):
            assert catalog["common"][key], f"common.{key} is empty in {locale}"
