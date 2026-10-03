# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Moodle Solver desktop app."""

from pathlib import Path

block_cipher = None
_spec_dir = Path(SPECPATH).resolve()
root = _spec_dir.parent if _spec_dir.name.lower() == "packaging" else _spec_dir

a = Analysis(
    [str(root / 'launch.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / 'gui' / 'static'), 'gui/static'),
        (str(root / 'config.example.yaml'), '.'),
        (str(root / 'prompts'), 'prompts'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'webview',
        'agents',
        'agents.registry',
        'agents.openai_compat',
        'agents.groq_agent',
        'agents.gemini_agent',
        'agents.cerebras_agent',
        'agents.deepseek_agent',
        'agents.openrouter_agent',
        'agents.github_agent',
        'gui.app',
        'gui.desktop',
        'remote.runner',
        'moodle.api',
        'moodle.scraper',
        'solver',
    ],
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
    [],
    exclude_binaries=True,
    name='MoodleSolver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MoodleSolver',
)
