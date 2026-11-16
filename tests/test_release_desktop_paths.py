"""release-desktop.sh resolves its own folder before it changes directory.

Run as ``./scripts/release-desktop.sh``, ``$(dirname "$0")`` is the relative
``./scripts``: once the script has done ``cd "$TAURI_DIR"`` it points nowhere.
The 1.2.30 release stopped on it, at the step that builds the notes.
"""
from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release-desktop.sh"


def test_the_scripts_folder_is_resolved_before_the_first_cd():
    lines = SCRIPT.read_text().splitlines()
    first_cd = next(i for i, line in enumerate(lines) if line.strip().startswith('cd "$TAURI_DIR"'))
    late = [i + 1 for i, line in enumerate(lines)
            if 'dirname "$0"' in line and i > first_cd and not line.lstrip().startswith("#")]
    assert not late, f'$(dirname "$0") used after `cd "$TAURI_DIR"` on line(s) {late}'


def test_the_notes_are_built_with_the_resolved_path():
    text = SCRIPT.read_text()
    assert 'NOTES_SCRIPT="$(cd "$(dirname "$0")" && pwd)/release_notes.py"' in text
    assert 'python3 "$NOTES_SCRIPT" "$VERSION" --out "$NOTES_FILE"' in text
