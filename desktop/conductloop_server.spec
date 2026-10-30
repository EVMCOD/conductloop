# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the bundled ConductLoop server (Tauri sidecar).

Build from the repo root:
    cd ~/ops-dashboard
    .venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm

Output: dist/conductloop-server/  (onedir) with the launcher binary +
the ops_dashboard package and its web/ assets bundled.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (  # noqa: F821
    collect_all, collect_dynamic_libs, collect_submodules)

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

# The local embedding engine: ONNX instead of torch, so the app embeds
# offline without carrying a 2 GB runtime. The model itself is downloaded
# once, on demand, into the data dir.
for _mod in ("fastembed", "onnxruntime", "tokenizers", "huggingface_hub"):
    _d, _b, _h = collect_all(_mod)
    datas += _d
    pkg_binaries += _b
    hidden += _h
pkg_binaries += collect_dynamic_libs("onnxruntime")
# fastembed imports these lazily, so the analyser never sees them: without
# them the engine cannot load a model.
hidden += ["numpy", "PIL", "PIL.Image", "PIL.ImageOps"]

a = Analysis(
    [str(ROOT / "desktop" / "server_entry.py")],
    pathex=[str(ROOT)],
    binaries=pkg_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # torch is out (GB of runtime for a model that runs on ONNX just as
    # well); numpy, PIL and the tokenizers stay, because the ONNX engine
    # needs them. embeddings.py degrades to keyword search if the engine is
    # missing all the same.
    excludes=[
        "tkinter", "matplotlib", "pandas",
        "torch", "torchvision", "torchaudio",
        "sentence_transformers", "transformers",
        "sklearn", "scipy", "matplotlib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Windows only: run the frozen interpreter in UTF-8 mode (the same as
# python -X utf8; PyInstaller bakes it into the executable).
# Without it open() without an encoding uses the ANSI code page (cp1252) there.
# The macOS and Linux builds get no options, as before.
options = [("X utf8", None, "OPTION")] if sys.platform == "win32" else []

# onefile: a single executable, so Tauri can ship it as an externalBin sidecar.
exe = EXE(
    pyz,
    a.scripts,
    options,
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
