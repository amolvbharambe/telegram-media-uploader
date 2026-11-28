#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
APP_NAME="Telegram Uploader"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BACKEND_DEST="$RESOURCES_DIR/backend"
ICONSET_DIR="$SCRIPT_DIR/AppIcon.iconset"

rm -rf "$APP_BUNDLE" "$ICONSET_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$BACKEND_DEST"

echo "1. Generating AppIcon.icns..."
swift "$SCRIPT_DIR/generate_icon.swift" "$ICONSET_DIR"
iconutil -c icns "$ICONSET_DIR" -o "$RESOURCES_DIR/AppIcon.icns"
rm -rf "$ICONSET_DIR"

echo "2. Compiling native macOS Swift binary..."
ARCH="$(uname -m)"
swiftc \
    -swift-version 5 \
    -O \
    -target "${ARCH}-apple-macosx13.0" \
    "$SCRIPT_DIR/Sources/main.swift" \
    -framework AppKit \
    -framework WebKit \
    -framework Foundation \
    -o "$MACOS_DIR/TelegramUploader"

chmod +x "$MACOS_DIR/TelegramUploader"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS_DIR/Info.plist"

echo "3. Bundling clean Python backend (excluding personal data, venv, caches)..."
rsync -a \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "$PROJECT_ROOT/app" "$BACKEND_DEST/"
cp "$PROJECT_ROOT/requirements.txt" "$BACKEND_DEST/"
cp "$PROJECT_ROOT/cli.py" "$BACKEND_DEST/"
cp "$PROJECT_ROOT/upgrade_to_streamable.py" "$BACKEND_DEST/"
cp "$PROJECT_ROOT/config.example.json" "$BACKEND_DEST/"

echo "4. Ad-hoc codesigning .app bundle..."
codesign --force --deep --sign - "$APP_BUNDLE"

echo "5. Creating distributable ZIP archive..."
ZIP_OUT="$PROJECT_ROOT/Telegram-Uploader-macOS.zip"
rm -f "$ZIP_OUT"
ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$ZIP_OUT"

echo "========================================================"
echo "Built Native macOS App: $APP_BUNDLE"
echo "Built macOS Release Archive: $ZIP_OUT"
echo "========================================================"
ls -lh "$APP_BUNDLE" "$ZIP_OUT"
