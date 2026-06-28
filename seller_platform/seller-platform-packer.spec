# -*- mode: python ; coding: utf-8 -*-
# Сборка форка упаковщика под Seller Platform.
# Запуск (из каталога seller_platform):
#   ..\.venv\Scripts\pyinstaller.exe seller-platform-packer.spec
# Результат: dist\seller-platform-packer.exe (windowed, без консоли).
# config.ini в сборку НЕ вшивается — кладётся рядом с .exe и редактируется на месте.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('PDFtoPrinter.exe', '.'),
        ('ding.wav', '.'),
    ],
    hiddenimports=['win32api'],
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
    a.binaries,
    a.datas,
    [],
    name='seller-platform-packer',
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
