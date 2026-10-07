# PyInstaller spec — Windows onedir 빌드 (onefile 보다 시작이 빠르고 백신 오탐이 적음)
# 사용: pyinstaller build/app.spec --noconfirm
import os
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "app", "ui"), "app/ui")],
    hiddenimports=["webview.platforms.edgechromium", "clr_loader", "pythonnet"],
    hookspath=[os.path.join(SPECPATH, "hooks")],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest",
              "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HiplazaMeetingNotes",
    icon=os.path.join(ROOT, "assets", "icon.ico"),
    console=False,
    upx=False,
    version=os.path.join(SPECPATH, "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="HiplazaMeetingNotes", upx=False)
