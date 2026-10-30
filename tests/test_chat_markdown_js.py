"""Unit test for the markdown renderer in web/chat.js (esc → inline → list →
md). Pulls those functions out of the IIFE and runs them under Node; skipped
when Node isn't installed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "chat.js"
NODE = shutil.which("node")

CASES = {
    # A bullet between numbered items used to close the <ol> and restart at 1.
    "split_ordered": "1. Ship it\n- note\n2. Tell people",
    "nested": "1. Ship it\n   - build\n   - test\n2. Tell people",
    "loose": "1. One\n\n2. Two",
    "source_cite": "Price is 12 dollars [obsidian:Pricing experiment.md] and [repo:tidepool].",
    "number_cite": "Fact [2].",
    "escaped": "[obsidian:<img src=x onerror=alert(1)>.md]",
}


def _render() -> dict:
    src = CHAT.read_text()
    start = src.index("  function esc(s)")
    helpers = src[start:src.index("  function tint(", start)]
    md_start = src.index("  function inline(s)")
    md_end = src.index("  // Re-render at most once per frame")
    script = helpers + src[md_start:md_end] + (
        "\nconst cases = JSON.parse(process.argv[1]); const out = {};"
        "\nfor (const k in cases) out[k] = md(cases[k]);"
        "\nprocess.stdout.write(JSON.stringify(out));")
    run = subprocess.run([NODE, "-e", script, json.dumps(CASES)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_chat_markdown_lists_and_citations():
    out = _render()
    assert out["split_ordered"] == '<ol><li>Ship it</li></ol><ul><li>note</li></ul><ol start="2"><li>Tell people</li></ol>'
    assert out["nested"] == "<ol><li>Ship it<ul><li>build</li><li>test</li></ul></li><li>Tell people</li></ol>"
    assert out["loose"] == "<ol><li>One</li><li>Two</li></ol>"
    assert '<span class="cite" title="obsidian:Pricing experiment.md">Pricing experiment</span>' in out["source_cite"]
    assert '<span class="cite" title="repo:tidepool">tidepool</span>' in out["source_cite"]
    assert out["number_cite"] == '<p>Fact <span class="cite">2</span>.</p>'
    # Escaped before formatting: the only tags are ours, the name stays text.
    assert re.findall(r"<(\w+)", out["escaped"]) == ["p", "span"]
    assert out["escaped"] == ('<p><span class="cite" title="obsidian:&lt;img src=x onerror=alert(1)&gt;.md">'
                              "&lt;img src=x onerror=alert(1)&gt;</span></p>")
