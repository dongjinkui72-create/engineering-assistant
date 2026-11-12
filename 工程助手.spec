# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    "win32com", "win32com.client", "pythoncom", "pywintypes",
    "pymupdf", "fitz", "ezdxf", "openai", "PIL.Image", "PIL.ImageOps",
    "numpy", "numpy.core._methods", "numpy.lib.format",
    "app.cad.nx", "app.cad.sw", "app.cad.catia",
    "app.cam.gcode", "app.cam.turning", "app.cam.others",
    "app.cam.verify", "app.cam.verify2", "app.cam.plan",
]
# hiddenimports += collect_submodules("win32com")

excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts", "PySide6.QtQuick",
    "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtDesigner", "PySide6.QtBluetooth",
    "matplotlib", "scipy", "pandas", "tkinter",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="工程助手",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, icon="assets/icon.ico",
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, upx_exclude=[],
    name="工程助手",
)