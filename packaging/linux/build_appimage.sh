#!/usr/bin/env bash
# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Wraps the PyInstaller folder into an AppImage.
#
#   packaging/linux/build_appimage.sh dist/openSciLab build/icons/openscilab-256.png openSciLab.AppImage
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
APPDIR="$WORK/openSciLab.AppDir"

mkdir -p "$APPDIR/usr/lib/openscilab" "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$DIST"/. "$APPDIR/usr/lib/openscilab/"
cp "$HERE/openscilab.desktop" "$APPDIR/openscilab.desktop"
cp "$HERE/openscilab.desktop" "$APPDIR/usr/share/applications/openscilab.desktop"
cp "$ICON" "$APPDIR/openscilab.png"
cp "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/openscilab.png"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/openscilab/openSciLab" "$@"
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
