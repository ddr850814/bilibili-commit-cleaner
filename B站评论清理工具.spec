# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('bilibili_reply_cleaner.py', '.')],
    hiddenimports=['qrcode'],
    excludes=['numpy', 'pygame', 'pandas', 'scipy', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'jupyter', 'notebook', 'pytest', 'django', 'flask', 'sqlalchemy', 'tornado', 'zmq'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    name='B站评论清理工具',
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
)
