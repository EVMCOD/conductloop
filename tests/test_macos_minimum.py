"""The macOS minimum is stated in several places; they must not drift apart.

The app needs macOS 13 (Ventura) to launch; the in-app browser needs macOS 14
(Sonoma) because of the Network.framework proxy symbols (weak-linked, so the
binary still loads on 13). The source of truth for "13" is `minimumSystemVersion`
in tauri.conf.json; for "14" it is the runtime gate in browser.rs. Everything a
person reads (READMEs, DISTRIBUTION.md, the release-notes template) must say
the same thing.

`desktop-demo/` is a separate marketing build with its own minimum and is
deliberately not covered here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI_CONF = ROOT / "desktop/src-tauri/tauri.conf.json"
BROWSER_RS = ROOT / "desktop/src-tauri/src/browser.rs"

# Files that tell people which macOS they need.
DOCS = [
    ROOT / "README.md",
    ROOT / "desktop/README.md",
    ROOT / "desktop/docs/DISTRIBUTION.md",
    ROOT / "scripts/release-desktop.sh",  # release-notes template
]
# Files whose comments talk about the minimum.
COMMENTED = [ROOT / "desktop/src-tauri/build.rs", ROOT / "desktop/src-tauri/Cargo.toml"]


def _app_minimum() -> int:
    conf = json.loads(TAURI_CONF.read_text())
    return int(conf["bundle"]["macOS"]["minimumSystemVersion"].split(".")[0])


def _browser_minimum() -> int:
    m = re.search(r"major\s*>=\s*(\d+)", BROWSER_RS.read_text())
    assert m, "browser.rs no longer has the `major >= N` macOS gate this test reads"
    return int(m.group(1))


def test_minimums_are_what_the_product_promises():
    assert _app_minimum() == 13
    assert _browser_minimum() == 14


def test_tauri_minimum_is_a_bare_major_dot_zero():
    conf = json.loads(TAURI_CONF.read_text())
    assert re.fullmatch(r"\d+\.0", conf["bundle"]["macOS"]["minimumSystemVersion"])


def test_every_doc_states_the_same_app_minimum():
    app = _app_minimum()
    for path in DOCS:
        text = path.read_text()
        mentioned = {int(n) for n in re.findall(r"macOS (\d+)\b", text)}
        assert app in mentioned, f"{path.relative_to(ROOT)} does not say 'macOS {app}'"


def test_every_doc_states_the_browser_minimum():
    browser = _browser_minimum()
    for path in DOCS:
        text = path.read_text()
        assert f"macOS {browser}" in text, (
            f"{path.relative_to(ROOT)} does not say the in-app browser needs 'macOS {browser}'"
        )


def test_no_doc_or_comment_mentions_a_stale_minimum():
    """No 'macOS 10.15 / 11 / 12', and no '10.15' left in the comments."""
    allowed = {_app_minimum(), _browser_minimum()}
    for path in DOCS + COMMENTED:
        text = path.read_text()
        for n in re.findall(r"macOS (\d+)\b", text):
            assert int(n) in allowed, (
                f"{path.relative_to(ROOT)} mentions 'macOS {n}', "
                f"expected only {sorted(allowed)}"
            )
        assert "10.15" not in text, f"{path.relative_to(ROOT)} still mentions 10.15"


def test_release_script_notes_template_names_both_versions():
    script = (ROOT / "scripts/release-desktop.sh").read_text()
    m = re.search(r"\*\*Requirements:\*\*\n(.*?)\" \\\n", script, re.S)
    assert m, "release notes 'Requirements' block not found in release-desktop.sh"
    block = m.group(1)
    assert f"macOS {_app_minimum()} (Ventura) or later" in block
    assert f"macOS {_browser_minimum()} Sonoma" in block
