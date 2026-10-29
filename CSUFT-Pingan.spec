# -*- mode: python ; coding: utf-8 -*-
"""Reproducible Windows build, including venvs created from Conda."""
import os
import re
import sys
from pathlib import Path

root = Path(SPECPATH)
# A venv created from Conda still loads Python extensions from base_prefix.
# PyInstaller cannot infer base_prefix/Library/bin from the venv's conda-meta.
# Give dependency collection the actual interpreter's DLL directories before
# Analysis, even when launched from a plain CMD with no activated Conda PATH.
base = Path(sys.base_prefix)
runtime_dirs = [path for path in (
    base / 'Library' / 'bin', base / 'DLLs', base,
) if path.is_dir()]
os.environ['PATH'] = os.pathsep.join(
    [str(path) for path in runtime_dirs] + [os.environ.get('PATH', '')]
)
a = Analysis(
    [str(root / 'app.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=['win32crypt', 'pywintypes'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt's current Windows build imports Microsoft's unversioned system ICU API.
# Conda ships a different icuuc.dll exporting versioned symbols. Collecting it
# shadows Windows/System32/icuuc.dll and makes QtCore fail before app startup.
# Windows system DLLs must stay provided by the OS, not copied from Conda.
a.binaries = [
    entry for entry in a.binaries
    if not re.fullmatch(r'icu(?:uc|in|dt\d+)\.dll', Path(entry[0]).name, re.I)
]
# Refuse a deceptively successful EXE if Conda's required runtime DLLs were lost.
bundled = {Path(entry[0]).name.casefold() for entry in a.binaries}
for name in ('ffi.dll', 'libmpdec-4.dll', 'libcrypto-3-x64.dll',
             'libssl-3-x64.dll', 'liblzma.dll', 'LIBBZ2.dll', 'libexpat.dll'):
    if (base / 'Library' / 'bin' / name).is_file() and name.casefold() not in bundled:
        raise RuntimeError('Required Python runtime DLL was not bundled: ' + name)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CSUFT-Pingan',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=os.environ.get('PINGAN_BUILD_CONSOLE') == '1',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
