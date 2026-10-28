"""Every script of the UI must at least parse.

A syntax error in a page's inline ``<script>`` kills that whole script: no
handler is attached and the page looks fine but does nothing. Two pages were
shipped like that (``/setup``: a ``const`` declared twice; ``/boardroom``: a
truncated function) and nothing noticed, because the Python tests never run
the pages' JavaScript.

This runs ``node --check`` on

* every inline ``<script>`` (no ``src``) of every page in ``ops_dashboard/web``,
* every static ``.js`` of the UI, and the scripts the desktop app injects
  into the WebView (``desktop/src-tauri/src/*.js``),

and checks that every local ``<script src>`` / ``<link rel=stylesheet>`` of a
page points to a file that exists (a missing stylesheet is served as HTML and
logs a console error on every visit).

Node is on the GitHub-hosted runners. Without node the tests skip on a dev
machine, but fail when ``$CI`` is set: a runner without node must not turn a
safety net into a silent pass.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
DESKTOP_JS = ROOT / "desktop" / "src-tauri" / "src"

NODE = shutil.which("node")

# <script type=...> values that are JavaScript. Anything else (json, templates,
# importmap) is data and is not parsed.
JS_TYPES = {"", "text/javascript", "application/javascript", "module"}


class _Page(HTMLParser):
    """Inline scripts (with the line they start at) and the local resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.inline: list[tuple[int, str, str]] = []   # (line, type, code)
        self.resources: list[str] = []
        self._cur: tuple[int, str] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "script":
            if a.get("src"):
                self.resources.append(a["src"])
            else:
                self._cur = (self.getpos()[0], a.get("type", "").strip().lower())
                self._buf = []
        elif tag == "link" and "stylesheet" in a.get("rel", "").lower() and a.get("href"):
            self.resources.append(a["href"])

    def handle_data(self, data):
        if self._cur is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._cur is not None:
            line, kind = self._cur
            self.inline.append((line, kind, "".join(self._buf)))
            self._cur = None


def _parse(path: Path) -> _Page:
    page = _Page()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


def _pages() -> list[Path]:
    return sorted(WEB.glob("*.html"))


def _static_js() -> list[Path]:
    files = sorted(p for p in WEB.rglob("*") if p.suffix in (".js", ".mjs"))
    files += sorted(DESKTOP_JS.rglob("*.js"))
    return files


def _check(code: str, tmp_path: Path, name: str, module: bool = False) -> str:
    """'' if the code parses, else node's message."""
    src = tmp_path / (name + (".mjs" if module else ".js"))
    src.write_text(code, encoding="utf-8")
    done = subprocess.run([NODE, "--check", str(src)], capture_output=True, text=True, timeout=60)
    return "" if done.returncode == 0 else done.stderr.strip()


@pytest.fixture(scope="module", autouse=True)
def _need_node():
    if NODE:
        return
    if os.environ.get("CI"):
        pytest.fail("node is required in CI to syntax-check the UI scripts")
    pytest.skip("node is not installed")


def test_there_is_something_to_check():
    inline = sum(1 for p in _pages() for _l, k, c in _parse(p).inline if c.strip() and k in JS_TYPES)
    assert len(_pages()) >= 20
    assert inline >= 15, "the scan found almost no inline scripts: the parser is broken"
    assert len(_static_js()) >= 20


def test_inline_scripts_parse(tmp_path):
    bad: list[str] = []
    for page in _pages():
        for n, (line, kind, code) in enumerate(_parse(page).inline):
            if kind not in JS_TYPES or not code.strip():
                continue
            err = _check(code, tmp_path, f"{page.stem}_{n}", module=kind == "module")
            if err:
                first = next((x for x in err.splitlines() if "Error" in x), err.splitlines()[0])
                bad.append(f"{page.name} (script at line {line}): {first}")
    assert not bad, "inline script with a syntax error:\n  " + "\n  ".join(bad)


def test_static_scripts_parse(tmp_path):
    bad: list[str] = []
    for n, path in enumerate(_static_js()):
        err = _check(path.read_text(encoding="utf-8"), tmp_path, f"static_{n}", module=path.suffix == ".mjs")
        if err:
            first = next((x for x in err.splitlines() if "Error" in x), err.splitlines()[0])
            bad.append(f"{path.relative_to(ROOT)}: {first}")
    assert not bad, "script with a syntax error:\n  " + "\n  ".join(bad)


def test_checker_catches_what_it_is_for(tmp_path):
    """The two mistakes that shipped, and a good script."""
    assert _check("function f() { const a = 1; const a = 2; }", tmp_path, "dup")
    assert _check("function formatAgo      const h = 1;\n  return h;\n}", tmp_path, "trunc")
    assert not _check("function formatAgo(ms) { const h = ms; return h; }", tmp_path, "ok")


def test_local_resources_of_every_page_exist():
    missing: list[str] = []
    for page in _pages():
        for ref in _parse(page).resources:
            if ref.startswith(("http:", "https:", "//", "data:")):
                continue
            rel = ref.split("?")[0].split("#")[0].lstrip("/")
            if not (WEB / rel).is_file():
                missing.append(f"{page.name}: {ref}")
    assert not missing, "page points to a file that does not exist:\n  " + "\n  ".join(missing)
