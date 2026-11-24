# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

hiddenimports = [
    "win32com", "win32com.client", "pythoncom", "pywintypes",
    "ezdxf", "openai", "PIL.Image", "PIL.ImageOps",
    "numpy", "numpy.lib.format",
    "app.cad.nx", "app.cad.sw",
    "app.local_ocr", "app.local_vision", "app.local_ai",
]
# 本地 OCR（离线识图）：把 rapidocr 的模型/字典和 onnxruntime 动态库一起收进来。
datas = [("assets", "assets"), ("app/_vision_impl.marshal", "app"), ("local_ai", "local_ai")]
binaries = []
for _pkg in ("rapidocr_onnxruntime", "onnxruntime"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception as _e:
        print(f"[spec] collect_all({_pkg}) failed: {_e}")
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
    binaries=binaries,
    datas=datas,
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
