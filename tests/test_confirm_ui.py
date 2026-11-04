"""No destructive action may ask the user through a browser global.

The desktop app (Tauri v2) overrides ``window.confirm`` with an async IPC
call to the dialog plugin, so in the app ``confirm()`` returns a **Promise**,
not a boolean. A truthiness guard -- ``if (!confirm(...)) return;`` -- then
sees a truthy promise, never fires, and the destructive action runs without
asking. In a browser the same line is correct, which is why only the desktop
app is affected and why browser-only tests never caught it.

Awaiting the global is not the fix either: the plugin's ``message`` command
needs the ``dialog:allow-message`` permission, which this repo grants to
NOBODY on purpose (``tests/test_tauri_capabilities.py``,
``test_the_dialog_plugin_commands_are_granted_to_nobody``). Without it the
awaited promise rejects with an ACL error instead of resolving ``false``.

So the question is asked in the page: ``web/confirm.js`` wraps a ``<dialog>``
opened with ``showModal()`` in a Promise. It needs no permission and behaves
the same in the browser and in the app.

These tests are the cheap guard that keeps the pattern from coming back.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
HELPER = WEB / "confirm.js"
STYLES = WEB / "confirm.css"
NODE = shutil.which("node")

# `confirm(` called as a bare global, or as `window.confirm(`. A member call
# (`dialog.confirm(...)`) is a different, working helper and is not matched,
# and neither is the definition of one (`function confirm(opts)`).
_BARE_CONFIRM = re.compile(r"(?<!function )(?<![\w.$])confirm\s*\(")
_WINDOW_CONFIRM = re.compile(r"window\s*\.\s*confirm\s*\(")

# The pages that can ask, and therefore load the helper. Each one is checked
# against the tree below, so a page that stops asking does not have to be
# removed from here by hand.
PAGES_WITH_HELPER = [
    "index.html", "connectors.html", "learning.html", "sessions.html",
    "setup.html", "media.html", "pair.html", "boardroom-thread.html",
]

# After an identifier, a digit or a closing bracket, a `/` is a division.
# Anywhere else it opens a regex literal.
_DIVISION_AFTER = set(")]}")
# After these keywords a `/` opens a regex literal, not a division.
_REGEX_AFTER_WORD = {
    "return", "typeof", "case", "in", "of", "new", "delete", "void", "do",
    "else", "instanceof", "yield", "await",
}


def _strip_js_code(src: str) -> str:
    """Return only the CODE of a JS source.

    Comments, string literals, template literals and regex literals are
    blanked out (newlines are kept, so line numbers stay honest).

    A naive "drop the // comments" pass mis-reads a regex like /['"]/ as a
    string literal and then reports code that is really inside a comment as
    if it were a call -- that is how ``chat.js`` names ``confirm()`` in a
    comment and the guard cries wolf. So this walks the source with a small
    state machine instead of a regex.
    """
    out: list[str] = []
    i, n = 0, len(src)
    # Stack of contexts: {"kind": "code"|"template"|"interp", "depth": int}
    stack = [{"kind": "code", "depth": 0}]
    last_significant = ""      # last code char that was not whitespace
    last_word = ""             # last identifier read in code

    def blank(text: str) -> str:
        return "".join("\n" if c == "\n" else " " for c in text)

    while i < n:
        top = stack[-1]
        kind = top["kind"]

        if kind == "code" or kind == "interp":
            ch = src[i]
            if ch == "/" and i + 1 < n and src[i + 1] == "/":
                end = src.find("\n", i)
                end = n if end < 0 else end
                out.append(blank(src[i:end]))
                i = end
                continue
            if ch == "/" and i + 1 < n and src[i + 1] == "*":
                end = src.find("*/", i + 2)
                end = n if end < 0 else end + 2
                out.append(blank(src[i:end]))
                i = end
                continue
            if ch in "'\"":
                quote = ch
                j = i + 1
                while j < n:
                    if src[j] == "\\":
                        j += 2
                        continue
                    if src[j] == quote or src[j] == "\n":
                        j += 1
                        break
                    j += 1
                out.append(blank(src[i:j]))
                i = j
                last_significant = ""
                last_word = ""
                continue
            if ch == "`":
                out.append(" ")
                i += 1
                stack.append({"kind": "template", "depth": 0})
                last_significant = ""
                last_word = ""
                continue
            if ch == "{" and kind == "interp":
                top["depth"] += 1
            elif ch == "}" and kind == "interp":
                if top["depth"] == 0:
                    # The interpolation closed: back to the template literal.
                    stack.pop()
                    out.append(" ")
                    i += 1
                    last_significant = ""
                    last_word = ""
                    continue
                top["depth"] -= 1
            if ch == "/" and not (
                last_significant in _DIVISION_AFTER
                or (last_significant.isalnum() or last_significant in "_$")
                and last_word not in _REGEX_AFTER_WORD
            ):
                # A regex literal: read to its closing slash.
                j = i + 1
                in_class = False
                while j < n:
                    c = src[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "\n":
                        break
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        j += 1
                        break
                    j += 1
                out.append(blank(src[i:j]))
                i = j
                last_significant = ""
                last_word = ""
                continue
            out.append(ch)
            if ch.isalnum() or ch in "_$":
                last_word = last_word + ch if last_word else ch
            elif not ch.isspace():
                last_word = ""
            if not ch.isspace():
                last_significant = ch
            i += 1
            continue

        # Inside a template literal.
        ch = src[i]
        if ch == "\\":
            out.append("  " if i + 1 < n and src[i + 1] != "\n" else " ")
            i += 2
            continue
        if ch == "`":
            out.append(" ")
            i += 1
            stack.pop()
            last_significant = ""
            last_word = ""
            continue
        if ch == "$" and i + 1 < n and src[i + 1] == "{":
            out.append("  ")
            i += 2
            stack.append({"kind": "interp", "depth": 0})
            continue
        out.append("\n" if ch == "\n" else " ")
        i += 1
    return "".join(out)


def _inline_scripts(html: str) -> list[str]:
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)


def _sources() -> list[tuple[str, str]]:
    """(name, code) of every script the UI runs."""
    out: list[tuple[str, str]] = []
    for p in sorted(WEB.glob("*.js")):
        out.append((p.name, _strip_js_code(p.read_text(encoding="utf-8"))))
    for p in sorted(WEB.glob("*.html")):
        for i, block in enumerate(_inline_scripts(p.read_text(encoding="utf-8"))):
            out.append((f"{p.name}#script{i}", _strip_js_code(block)))
    return out


def _page_scripts(page: str, exclude_helper: bool = False) -> str:
    """The code a page runs: its local scripts plus its inline ones.

    ``exclude_helper`` drops web/confirm.js: the helper itself contains
    "confirmAction(", so counting it would make "this page asks" true for
    every page that merely loads the helper -- an assertion that passes when
    the thing it guards is broken.
    """
    html = (WEB / page).read_text(encoding="utf-8")
    parts = []
    for src in re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", html):
        f = WEB / src.split("?")[0].lstrip("/")
        if not f.exists():
            continue
        if exclude_helper and f == HELPER:
            continue
        parts.append(_strip_js_code(f.read_text(encoding="utf-8")))
    parts.extend(_strip_js_code(b) for b in _inline_scripts(html))
    return "\n".join(parts)


def test_the_comment_stripper_sees_code_and_not_prose():
    """The guard is only as good as the pass that feeds it.

    A comment that names the pattern must not look like a call, a regex that
    contains a quote must not swallow the rest of the file, and a string that
    contains the pattern must not look like code either.
    """
    src = """
      // no confirm() dialogs in the app webview
      /* confirm( in a block comment */
      var quoted = "confirm(";
      var regex = /['"]/g;
      var real = confirm('x');
    """
    code = _strip_js_code(src)
    # The comment and the string literal are blanked out, so neither reads as
    # a call; the real call survives as code (its argument is blanked too --
    # that is the point of the lexer).
    assert "confirm(" not in code.split("var real")[0], code
    assert "confirm(" in code.split("var real")[1], code
    assert "'x'" not in code, code


def test_no_page_asks_through_the_browser_global():
    """The bug itself: a truthiness guard on a global the shell overrides."""
    hits = []
    for name, code in _sources():
        for regex in (_BARE_CONFIRM, _WINDOW_CONFIRM):
            for m in regex.finditer(code):
                line = code[: m.start()].count("\n") + 1
                hits.append(f"{name}:{line}: {m.group(0).strip()}")
    assert not hits, (
        "ask in the page with confirmAction() (web/confirm.js): in the desktop "
        "app confirm() is a Promise, so the guard passes and the action runs "
        "unasked:\n" + "\n".join(hits))


# The other two globals the shell breaks: window.alert is overridden with an
# IPC call that has no permission, and wry ships no text-input panel at all,
# so a prompt is a silent empty string. Both are asked in the page now.
_BARE_ALERT = re.compile(r"(?<![\w.$])alert\s*\(")
_BARE_PROMPT = re.compile(r"(?<![\w.$])prompt\s*\(")
_WINDOW_ALERT = re.compile(r"window\s*\.\s*alert\s*\(")
_WINDOW_PROMPT = re.compile(r"window\s*\.\s*prompt\s*\(")


def test_no_page_reports_through_the_browser_globals():
    """alert() and prompt() are broken in the desktop app the same way.

    alert() is overridden with an IPC call the app has no permission for, so
    the message goes nowhere. prompt() is not overridden, but wry ships no
    text-input panel: the value comes back empty and the flow stops without
    the user ever being asked. Both are asked in the page now.
    """
    hits = []
    for name, code in _sources():
        for regex in (_BARE_ALERT, _WINDOW_ALERT, _BARE_PROMPT, _WINDOW_PROMPT):
            for m in regex.finditer(code):
                line = code[: m.start()].count("\n") + 1
                hits.append(f"{name}:{line}: {m.group(0).strip()}")
    assert not hits, (
        "report in the page: notify() for a message (web/toast.js) and "
        "promptAction() for an input (web/confirm.js): in the desktop app "
        "alert() goes nowhere and prompt() always comes back empty:\n"
        + "\n".join(hits))


def test_the_helper_exists_and_asks_in_the_page():
    assert HELPER.is_file(), "web/confirm.js is missing: there is no in-page confirmation"
    src = HELPER.read_text(encoding="utf-8")
    assert "showModal" in src, "the helper must ask with a <dialog> opened with showModal()"
    assert "confirmAction" in src, "the helper must expose confirmAction()"


def test_the_helper_needs_no_inline_code():
    """Strict-CSP pages (sessions, connectors, learning) must load it with zero inline code."""
    src = _strip_js_code(HELPER.read_text(encoding="utf-8"))
    assert not re.search(r"\son[a-z]{3,}\s*=\s*[\"']", src), "inline event handler"
    assert not re.search(r"\sstyle\s*=\s*[\"']", src), "inline style attribute"
    assert not re.search(r"setAttribute\(\s*[\"'](?:style|on[a-z]+)[\"']", src), "inline style/handler"


def test_the_helper_ships_its_own_styles():
    """A shared primitive must render on every page it is loaded on."""
    assert STYLES.is_file(), "web/confirm.css is missing: the dialog would render unstyled"
    css = STYLES.read_text(encoding="utf-8")
    assert "dialog.confirm" in css, "the stylesheet must style the dialog the helper opens"


def test_every_page_that_asks_loads_the_helper():
    """A page whose code asks must load the helper before that code runs."""
    for page in PAGES_WITH_HELPER:
        html = (WEB / page).read_text(encoding="utf-8")
        assert "/confirm.js" in html, f"{page} does not load /confirm.js"
        assert "/confirm.css" in html, f"{page} does not load /confirm.css"
        assert "confirmAction(" in _page_scripts(page, exclude_helper=True), (
            f"{page} no longer asks anything: remove it from the list of pages "
            f"that load the helper")
    for page in PAGES_WITH_HELPER:
        html = (WEB / page).read_text(encoding="utf-8")
        if "/app.js" in html:
            assert html.index("/confirm.js") < html.index("/app.js"), (
                f"{page}: the helper must be loaded before the code that asks")


def test_the_helper_is_valid_javascript():
    assert NODE is not None, "node is not installed"
    chk = subprocess.run([NODE, "--check", str(HELPER)], capture_output=True, text=True, timeout=60)
    assert chk.returncode == 0, chk.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_helper_answers_with_the_users_choice_and_never_asks_the_shell():
    """The primitive itself: a Promise around a <dialog>, no window.confirm.

    The fake DOM is the smallest one that can run the helper; the browser
    test (tests/test_confirm_ui_browser.py) proves the same contract against
    a real page.
    """
    src = HELPER.read_text(encoding="utf-8")
    script = """
const fake = (tag) => ({
  tagName: tag, className: '', textContent: '', id: '', type: '', children: [],
  parentNode: null, open: false, _l: {},
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
  removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; return c; },
  addEventListener(ev, fn) { (this._l[ev] = this._l[ev] || []).push(fn); },
  close() { this.open = false; },
  showModal() { this.open = true; },
});
const body = fake('body');
global.document = { body: body, createElement: (t) => fake(t) };
global.window = global;
let shellAsked = 0;
global.confirm = function () { shellAsked++; return Promise.resolve(true); };
__SRC__
const find = (root, id) => {
  for (const c of root.children) {
    if (c.id === id) return c;
    const hit = find(c, id);
    if (hit) return hit;
  }
  return null;
};
const click = (root, id) => (find(root, id)._l['click'] || []).forEach(fn => fn());
(async () => {
  const p = window.confirmAction('Delete this todo?');
  const dlg = body.children[body.children.length - 1];
  const msg = dlg.children[0].textContent;
  const opened = !!dlg.open;
  click(dlg, 'confirm-yes');
  const ok = await p;
  const removed = !dlg.parentNode;

  const p2 = window.confirmAction('Again?');
  const dlg2 = body.children[body.children.length - 1];
  click(dlg2, 'confirm-no');
  const no = await p2;

  const p3 = window.confirmAction('Escape?');
  const dlg3 = body.children[body.children.length - 1];
  (dlg3._l['cancel'] || []).forEach(fn => fn({ preventDefault() {} }));
  const esc = await p3;

  const p4 = window.confirmAction('Backdrop?');
  const dlg4 = body.children[body.children.length - 1];
  (dlg4._l['click'] || []).forEach(fn => fn({ target: dlg4 }));
  const backdrop = await p4;

  process.stdout.write(JSON.stringify({
    msg: msg, ok: ok, no: no, esc: esc, backdrop: backdrop,
    shellAsked: shellAsked, opened: opened, removed: removed,
  }));
})();
""".replace("__SRC__", src)
    run = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["msg"] == "Delete this todo?", out
    assert out["ok"] is True, out
    assert out["no"] is False, out
    assert out["esc"] is False, out
    assert out["backdrop"] is False, (out, "clicking the backdrop must answer 'no'")
    assert out["shellAsked"] == 0, "the helper asked the shell instead of the page"
    assert out["opened"] is True, out
    assert out["removed"] is True, "the dialog stays in the page after the answer"
