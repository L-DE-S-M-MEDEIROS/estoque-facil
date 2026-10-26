# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path
import ssl

datas = [('assets/brand', 'assets/brand')]
# This Python distribution loads an additional CA file when constructing any
# SSLContext. Frozen ssl.__file__ points at the extraction root, so retain it
# there as well; otherwise every HTTPS request fails before making a connection.
runtime_ca = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
if runtime_ca.is_file():
    datas.append((str(runtime_ca), '.'))
binaries = []
hiddenimports = []
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['openpyxl', 'pdfplumber.display', 'pypdfium2', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Estoque Bolsas Baby',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/brand/estoque-bolsas-baby.ico'],
)
