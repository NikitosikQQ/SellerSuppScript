# -*- mode: python ; coding: utf-8 -*-
# ОТЛАДОЧНАЯ сборка: то же, что auth_gui.spec, плюс вшитый diag_build.flag — он включает
# подробный лог diag.py (сеть по фазам, печать, шаги скана, падения) в файл рядом с exe.
# Сборка (в отдельные папки, чтобы не затереть релизный dist/auth_gui.exe):
#   .venv/Scripts/python.exe -m PyInstaller auth_gui_diag.spec --noconfirm --distpath dist-diag --workpath build-diag
# В цех как постоянную НЕ катить.


a = Analysis(
    ['auth_gui.py'],
    pathex=[],
    binaries=[],
     datas=[
        ('ding.wav', '.'),
        ('PDFtoPrinter.exe', '.'),
        ('diag_build.flag', '.'),
    ],
    # PIL — см. auth_gui.spec; win32print — снимки состояния принтера в diag.py.
    hiddenimports=['qrcode', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'win32print'],
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
    name='SS-DIAG',
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
