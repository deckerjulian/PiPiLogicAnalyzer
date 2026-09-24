#!/usr/bin/env bash
# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Builds the firmware for every board (and turbo variant) into firmware/uf2/.
#
# Uses the Pico SDK 2.1.1 and the tools installed by the "Raspberry Pi Pico" VS Code
# extension in ~/.pico-sdk; PICO_SDK_PATH / PICO_TOOLCHAIN_PATH override them.
#
#   firmware/build_all.sh                      all boards
#   firmware/build_all.sh BOARD_PICO BOARD_PICO_2   selected boards
#
# The images are named PiPiLogicAnalyzer_<BOARD_TYPE>[_Turbo].uf2, which the application
# uses to recognise the board when flashing.

set -eo pipefail

FIRMWARE_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$FIRMWARE_DIR/PiPiLogicAnalyzer"
OUTPUT_DIR="$FIRMWARE_DIR/uf2"
SETTINGS="$SOURCE_DIR/PiPiLogicAnalyzer_Build_Settings.cmake"
# Separate from PiPiLogicAnalyzer/build, so the VS Code build keeps its board in the cache
BUILD_DIR="$OUTPUT_DIR/.build"
ALL_BOARDS=(BOARD_PICO BOARD_PICO_2 BOARD_PICO_W BOARD_PICO_W_WIFI BOARD_PICO_2_W BOARD_PICO_2_W_WIFI BOARD_ZERO BOARD_INTERCEPTOR)

PICO_HOME="$HOME/.pico-sdk"
export PICO_SDK_PATH="${PICO_SDK_PATH:-$PICO_HOME/sdk/2.1.1}"
export PICO_TOOLCHAIN_PATH="${PICO_TOOLCHAIN_PATH:-$PICO_HOME/toolchain/14_2_Rel1}"

# Tools of the VS Code extension first in the PATH
for directory in "$PICO_TOOLCHAIN_PATH/bin" "$PICO_HOME"/cmake/*/bin "$PICO_HOME"/ninja/*; do
    if [ -d "$directory" ]; then
        PATH="$directory:$PATH"
    fi
done
export PATH

if [ ! -d "$PICO_SDK_PATH" ]; then
    echo "Pico SDK not found at $PICO_SDK_PATH" >&2
    echo "Install it with the Raspberry Pi Pico VS Code extension or set PICO_SDK_PATH." >&2
    exit 1
fi

for tool in cmake ninja arm-none-eabi-gcc; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "$tool not found (checked the PATH and $PICO_HOME)" >&2
        exit 1
    fi
done

if [ "$#" -gt 0 ]; then
    BOARDS=("$@")
else
    BOARDS=("${ALL_BOARDS[@]}")
fi

# CMakeLists.txt silently falls back to the Pico for unknown boards
for board in "${BOARDS[@]}"; do
    case " ${ALL_BOARDS[*]} " in
        *" $board "*) ;;
        *)
            echo "Unknown board $board, known: ${ALL_BOARDS[*]}" >&2
            exit 1
            ;;
    esac
done

# A leftover copy means an earlier run was killed before it could restore the settings
if [ -e "$SETTINGS.orig" ]; then
    echo "$SETTINGS.orig exists from an interrupted run." >&2
    echo "Move it back over $(basename "$SETTINGS") or delete it, then start again." >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
cp "$SETTINGS" "$SETTINGS.orig"
trap 'mv "$SETTINGS.orig" "$SETTINGS"' EXIT

built=0
failed=0

for board in "${BOARDS[@]}"; do
    for turbo in 0 1; do
        case "$board" in
            BOARD_PICO_W*|BOARD_PICO_2_W*)
                # CMakeLists.txt refuses turbo mode for the W boards
                if [ "$turbo" = 1 ]; then continue; fi
                ;;
        esac

        name="PiPiLogicAnalyzer_${board}"
        if [ "$turbo" = 1 ]; then name="${name}_Turbo"; fi
        log="$OUTPUT_DIR/$name.log"
        echo "==> $name"

        sed -e "s/^set(BOARD_TYPE .*)/set(BOARD_TYPE \"$board\")/" \
            -e "s/^set(TURBO_MODE .*)/set(TURBO_MODE $turbo)/" \
            "$SETTINGS.orig" > "$SETTINGS"

        # The board is cached by CMake, every variant needs a fresh build directory
        rm -rf "$BUILD_DIR"

        if cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -G Ninja > "$log" 2>&1 \
            && cmake --build "$BUILD_DIR" >> "$log" 2>&1; then
            cp "$BUILD_DIR/PiPiLogicAnalyzer.uf2" "$OUTPUT_DIR/$name.uf2"
            rm -f "$log"
            built=$((built + 1))
        else
            echo "    failed, see $log" >&2
            failed=$((failed + 1))
        fi
    done
done

rm -rf "$BUILD_DIR"

echo
echo "$built image(s) in $OUTPUT_DIR, $failed failed."
[ "$failed" = 0 ]
