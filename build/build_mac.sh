#!/bin/bash
# macOS 빌드: syscap(Swift) → PyInstaller .app → ad-hoc 서명 → DMG
# 사용: build/build_mac.sh [버전]   (Xcode Command Line Tools + Python 3.12 필요)
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:-${APP_VERSION:-1.1.0}}"
ARCH="$(uname -m)"
export APP_VERSION="$VERSION"
APP="dist/Hiplaza 회의록.app"

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt "pyinstaller==6.*"

echo "▶ syscap 빌드 ($ARCH)"
xcrun swiftc -O -target "${ARCH}-apple-macos13.0" macos/syscap.swift -o macos/syscap

echo "▶ PyInstaller"
rm -rf "$APP" dist/HiplazaMeetingNotes
pyinstaller build/mac.spec --noconfirm --distpath dist --workpath build/work-mac

echo "▶ ad-hoc 서명"
codesign --force --deep --sign - "$APP"
codesign --verify --deep "$APP"

echo "▶ DMG"
DMG="dist/HiplazaMeetingNotes-macOS-${ARCH}-${VERSION}.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Hiplaza 회의록" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
echo "완료: $DMG"
