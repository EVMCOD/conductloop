# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the bundled ConductLoop server (Tauri sidecar).

Build from the repo root:
    cd ~/ops-dashboard
    .venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm

Output: dist/conductloop-server/  (onedir) with the launcher binary +
the ops_dashboard package and its web/ assets bundled.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules  # noqa: F821

ROOT = Path(SPECPATH).parent  # repo root (spec lives in desktop/)  # noqa: F821

# Collect the whole app package (string-imported submodules aren't traced).
pkg_datas, pkg_binaries, pkg_hidden = collect_all("ops_dashboard")

# The web UI (html/js/json/i18n) is plain-file data — make sure it's bundled at
# ops_dashboard/web so STATIC_DIR = Path(__file__).parent/"web" resolves.
web_src = ROOT / "ops_dashboard" / "web"
datas = pkg_datas + [(str(web_src), "ops_dashboard/web")]

hidden = (
    pkg_hidden
    + collect_submodules("ops_dashboard")
    + ["typer", "rich", "httpx", "segno", "sqlite3"]
)

a = Analysis(
    [str(ROOT / "desktop" / "server_entry.py")],
    pathex=[str(ROOT)],
    binaries=pkg_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # The embedder (sentence-transformers → torch) is optional; embeddings.py
    # degrades to keyword search when it's absent. Excluding the whole ML stack
    # keeps the desktop bundle small (MB, not GB).
    excludes=[
        "tkinter", "matplotlib", "pandas",
        "torch", "torchvision", "torchaudio",
        "sentence_transformers", "transformers", "tokenizers", "safetensors",
        "huggingface_hub", "sklearn", "scipy", "numpy", "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onefile: a single executable, so Tauri can ship it as an externalBin sidecar.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="conductloop-server",
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
