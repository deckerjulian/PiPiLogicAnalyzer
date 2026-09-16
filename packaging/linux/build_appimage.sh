#!/usr/bin/env bash
# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Wraps the PyInstaller folder into an AppImage.
#
#   packaging/linux/build_appimage.sh dist/PiPiLogicAnalyzer build/icons/pipilogicanalyzer-256.png PiPiLogicAnalyzer.AppImage
#
# appimagetool is downloaded unless APPIMAGETOOL points to it. It runs without FUSE
# (--appimage-extract-and-run), so it also works in containers and CI runners.

set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <PyInstaller folder> <icon png> <output AppImage>" >&2
    exit 1
fi

DIST="$1"
ICON="$2"
OUTPUT="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/PiPiLogicAnalyzer.AppDir"

mkdir -p "$APPDIR/usr/lib/pipilogicanalyzer" "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$DIST"/. "$APPDIR/usr/lib/pipilogicanalyzer/"
cp "$HERE/pipilogicanalyzer.desktop" "$APPDIR/pipilogicanalyzer.desktop"
cp "$HERE/pipilogicanalyzer.desktop" "$APPDIR/usr/share/applications/pipilogicanalyzer.desktop"
cp "$ICON" "$APPDIR/pipilogicanalyzer.png"
cp "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/pipilogicanalyzer.png"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/pipilogicanalyzer/PiPiLogicAnalyzer" "$@"
EOF
chmod +x "$APPDIR/AppRun"

TOOL="${APPIMAGETOOL:-$WORK/appimagetool}"
if [ ! -x "$TOOL" ]; then
    curl -fsSL -o "$TOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi

ARCH=x86_64 "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUTPUT"
echo "AppImage written to $OUTPUT"
