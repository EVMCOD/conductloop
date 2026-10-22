"""Invisible-text filtering in the page scripts (follow-up to browser PR 10):
runs agent_snapshot.js and browser_extract.js (with visibility.js inlined as
Rust does) in a real headless Chrome on a local trap page. Skipped when no
Chrome/Chromium is installed. No network: the page is a local file."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src"
CHROME = next((p for p in (os.environ.get("CHROME_BIN"), shutil.which("google-chrome"),
                           shutil.which("chromium"), shutil.which("chromium-browser")) if p), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="no headless Chrome/Chromium")

TRAP = """
<style>
  body { margin: 0; font: 16px sans-serif; background: #fff; color: #111 }
  .sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap }
</style>
<main>
  <h1>V-TITLE</h1>
  <p>V-PLAIN visible text.</p>
  <p style="display:none">T-DISPLAY</p>
  <div style="display:none"><p>T-DISPLAY-INHERITED</p></div>
  <p style="visibility:hidden">T-VISIBILITY</p>
  <div style="visibility:hidden"><span>T-VISIBILITY-INHERITED</span></div>
  <p style="opacity:0">T-OPACITY</p>
  <div style="opacity:0"><p>T-OPACITY-ANCESTOR</p></div>
  <p style="font-size:0">T-FONT-ZERO</p>
  <p style="position:absolute;left:-9999px">T-OFFSCREEN-LEFT</p>
  <p style="position:absolute;top:-9999px">T-OFFSCREEN-TOP</p>
  <p style="text-indent:-9999px">T-TEXT-INDENT</p>
  <p style="position:absolute;clip:rect(0 0 0 0)">T-CLIP</p>
  <p style="clip-path:inset(50%)">T-CLIP-PATH</p>
  <p style="clip-path:circle(0)">T-CLIP-CIRCLE</p>
  <div style="width:0;height:0;overflow:hidden">T-ZERO-SIZE</div>
  <span class="sr-only">T-SR-ONLY</span>
  <p style="color:#fff">T-WHITE-ON-WHITE</p>
  <p style="color:transparent">T-TRANSPARENT</p>
  <div style="background:#123456"><p style="color:#123457">T-SAME-AS-PARENT-BG</p></div>
  <p aria-hidden="true">T-ARIA</p>
  <p hidden>T-HIDDEN-ATTR</p>
  <p style="transform:scale(0)">T-SCALE-ZERO</p>
  <div style="background:#000"><p style="color:#fff">V-WHITE-ON-BLACK</p></div>
  <div style="background-image:linear-gradient(#000,#000)"><p style="color:#fff">V-ON-IMAGE</p></div>
  <div style="font-size:0"><span style="font-size:16px">V-INLINE-BLOCK-TRICK</span></div>
  <button>V-BUY<span style="display:none">T-IN-BUTTON</span></button>
  <a href="https://example.com/x">V-LINK<span style="position:absolute;left:-9999px">T-IN-LINK</span></a>
  <p>V-END of the article. Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do
  eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis
  nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
</main>
"""
TRAPS = sorted(set(re.findall(r"T-[A-Z0-9-]+", TRAP)))
VISIBLE = sorted(set(re.findall(r"V-[A-Z0-9-]+", TRAP)))


def _script(name: str) -> str:
    s = (SRC / name).read_text(encoding="utf-8")
    s = s.replace("/*__READABILITY__*/", (SRC / "vendor" / "Readability.js").read_text(encoding="utf-8"), 1)
    return s.replace("/*__CL_VISIBILITY__*/", (SRC / "visibility.js").read_text(encoding="utf-8"), 1)


def _run(tmp_path: Path, name: str) -> dict:
    page = tmp_path / "trap.html"
    runner = ("<script>window.addEventListener('load', function () {"
              "var r; try { r = " + _script(name) + "; } catch (e) { r = {error: String(e)}; }"
              "var o = document.createElement('textarea'); o.id = 'cl-out';"
              "o.textContent = JSON.stringify(r); document.documentElement.appendChild(o); });</script>")
    page.write_text(f"<!doctype html><html><head><title>Trap</title></head><body>{TRAP}{runner}"
                    "</body></html>", encoding="utf-8")
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
         "--disable-extensions", "--disable-background-networking", "--window-size=1280,900",
         f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=3000", "--dump-dom",
         page.as_uri()], capture_output=True, text=True, timeout=90, check=False)
    m = re.search(r'<textarea id="cl-out">(.*?)</textarea>', out.stdout, re.S)
    assert m, out.stderr[-2000:]
    res = json.loads(html.unescape(m.group(1)))
    assert "error" not in res, res
    return res


def test_agent_snapshot_drops_invisible_text(tmp_path):
    r = _run(tmp_path, "agent_snapshot.js")
    names = " ".join(e["name"] for e in r["elements"])
    blob = r["text"] + "\n" + names
    leaked = [t for t in TRAPS if t in blob]
    assert not leaked, leaked
    missing = [v for v in VISIBLE if v not in r["text"]]
    assert not missing, missing
    assert "V-BUY" in names and "V-LINK" in names
    hid = r["hidden"]
    assert hid["total"] >= len(TRAPS) - 2   # T-IN-* live inside visible elements
    for why in ("display", "visibility", "opacity", "font_size", "offscreen", "text_indent",
                "clip", "clip_path", "size", "color", "aria_hidden", "hidden_attr", "transform"):
        assert hid["reasons"].get(why), (why, hid)


def test_extraction_drops_invisible_text(tmp_path):
    r = _run(tmp_path, "browser_extract.js")
    blob = r["html"] + "\n" + r["text"]
    leaked = [t for t in TRAPS if t in blob]
    assert not leaked, leaked
    for v in ("V-PLAIN", "V-END", "V-WHITE-ON-BLACK", "V-ON-IMAGE", "V-INLINE-BLOCK-TRICK"):
        assert v in blob, v
    assert r["hidden"]["total"] >= len(TRAPS) - 2


def test_nodes_past_the_scan_limit_are_dropped_not_trusted(tmp_path):
    # Past MAX_NODES nothing is inspected: that tail must not reach the vault as
    # if it were visible (Codex on #51).
    real = _script
    globals()["_script"] = lambda name: real(name).replace("MAX_NODES = 50000", "MAX_NODES = 3")
    try:
        res = _run(tmp_path, "browser_extract.js")
    finally:
        globals()["_script"] = real
    assert "V-END" not in res["text"]
    assert res["hidden"]["reasons"].get("uninspected", 0) > 0
