# PyInstaller spec — macOS .app 번들
# 사전: swiftc 로 macos/syscap 빌드 →  pyinstaller build/mac.spec --noconfirm
import os
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("APP_VERSION", "1.1.0")

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=[(os.path.join(ROOT, "macos", "syscap"), ".")],
    datas=[(os.path.join(ROOT, "app", "ui"), "app/ui")],
    hiddenimports=["webview.platforms.cocoa"],
    hookspath=[os.path.join(SPECPATH, "hooks")],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest",
              "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef",
              "webview.platforms.winforms", "webview.platforms.edgechromium", "PyQt5", "PyQt6", "PySide6"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="HiplazaMeetingNotes",
          console=False, upx=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="HiplazaMeetingNotes", upx=False)
app = BUNDLE(
    coll,
    name="Hiplaza 회의록.app",
    icon=os.path.join(ROOT, "assets", "icon.icns"),
    bundle_identifier="com.hiplaza.meetingnotes",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Hiplaza 회의록",
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "회의 중 내 목소리를 받아 적기 위해 마이크를 사용합니다.",
        "NSScreenCaptureUsageDescription": "화상회의 상대방의 목소리(시스템 소리)를 받아 적기 위해 사용합니다. 화면은 녹화하지 않습니다.",
        "NSAudioCaptureUsageDescription": "화상회의 상대방의 목소리(시스템 소리)를 받아 적기 위해 사용합니다.",
    },
)
