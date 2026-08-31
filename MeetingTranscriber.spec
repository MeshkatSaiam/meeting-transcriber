# -*- mode: python ; coding: utf-8 -*-

import os
import sys
sys.setrecursionlimit(5000)

from kivy_deps import sdl2, glew

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Projects\\meeting-transcriber\\README.md', '.')],
    hiddenimports=['kivy', 'sounddevice', 'soundfile', 'google.generativeai'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MeetingTranscriber',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    *[Tree(p) for p in (sdl2.dep_bins + glew.dep_bins)],
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MeetingTranscriber',
)
