# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AI Switch (Windows / macOS / Linux)

import sys
from pathlib import Path

block_cipher = None
# SPECPATH is the directory that contains this .spec file (packaging/)
_spec_dir = Path(SPECPATH).resolve()
if _spec_dir.is_file():
    _spec_dir = _spec_dir.parent
root = _spec_dir.parent  # project root

datas = [
    (str(root / "templates"), "templates"),
    (str(root / "static"), "static"),
    (str(root / "locales"), "locales"),
    (str(root / "VERSION"), "."),
]

hiddenimports = [
    "json5",
    "yaml",
    "flask",
    "requests",
    "backends",
    "backends.base",
    "backends.openclaw",
    "backends.opencode",
    "backends.claude_code",
    "backends.codex_cli",
    "backends.cline",
    "backends.aider",
    "backends.continue_dev",
    "backends.hermes",
    "backends.qwencode",
    "backends.goose",
    "backends.grok_cli",
    "backends.cursor_cli",
    "backends.copilot_cli",
    "backends.antigravity",
    "backends.devin",
    "core",
    "core.data",
    "core.i18n",
    "core.providers",
    "core.health_checker",
    "core.batch_import",
    "core.usage_import",
    "core.pricing",
    "core.paths",
    "core.version",
    "core.remote",
]

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ai-switch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
