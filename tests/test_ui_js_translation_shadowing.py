"""A loop variable or a local named like the translation function hides it.

chat.js named a thread `t` inside the code that also called t('chat.…'): the
list of past conversations threw "t is not a function" as soon as there was
one, and reopening a team-mode conversation threw the same. Nothing in the
page said so. This scans every script of the app for a parameter or a local
called `t` or `tr` in a function that then calls t('…') / tr('…').
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
NAMES = ("t", "tr")


def _body(src: str, start: int) -> tuple[str, int]:
    j = src.index("{", start)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1], j
    return src[j:], j


def _calls(name: str, text: str) -> bool:
    return re.search(r"(?<![\w.$])" + name + r"\(['\"]", text) is not None


def shadowing(src: str) -> list[str]:
    found = []
    for name in NAMES:
        if not _calls(name, src):
            continue
        for m in re.finditer(r"function\s*[\w$]*\s*\(([^)]*)\)", src):
            if name in [p.strip() for p in m.group(1).split(",")]:
                body, _ = _body(src, m.end())
                if _calls(name, body):
                    found.append(f"line {src[:m.start()].count(chr(10)) + 1}: parameter '{name}'")
        for m in re.finditer(r"(?:var|let|const)\s+" + name + r"\s*=\s*([^;\n]*)", src):
            init = m.group(1)
            if "_i18n" in init or init.lstrip().startswith(("(key", "function", "key =>")):
                continue  # this is the translation function itself
            k = src.rfind("function", 0, m.start())
            if k < 0:
                continue
            body, j = _body(src, k)
            if j < m.start() < j + len(body) and _calls(name, body[m.start() - j:]):
                found.append(f"line {src[:m.start()].count(chr(10)) + 1}: local '{name}'")
    return found


def test_no_script_hides_the_translation_function():
    problems = {}
    for js in sorted(WEB.glob("*.js")):
        hits = shadowing(js.read_text(encoding="utf-8"))
        if hits:
            problems[js.name] = hits
    assert not problems, problems


def test_the_check_finds_the_chat_bug():
    bad = """function t(k) { return k; }
    function list(rows) { rows.forEach(function (t) { el.title = t('chat.delete_thread'); }); }"""
    assert shadowing(bad)
    bad_local = """function t(k) { return k; }
    async function open() { var t = data.thread; done(t('chat.done')); }"""
    assert shadowing(bad_local)
