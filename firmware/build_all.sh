#!/usr/bin/env bash
# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Builds the firmware of the instruments.
#
#   firmware/build_all.sh                          everything there is (today: the Pico images)
#   firmware/build_all.sh --pico                   every Pico board (and turbo variant)
#   firmware/build_all.sh --pico BOARD_PICO BOARD_PICO_2   selected boards
#   firmware/build_all.sh BOARD_PICO               the same (boards alone mean --pico)
#   firmware/build_all.sh --arduino                the Arduino firmware (firmware/arduino, PlatformIO)
#   firmware/build_all.sh --bridge                 the bridge app (firmware/bridge-android, Gradle)
#
# Pico: uses the Pico SDK 2.1.1 and the tools in ~/.pico-sdk (the layout of the "Raspberry Pi
# Pico" VS Code extension and of the dev container); PICO_SDK_PATH / PICO_TOOLCHAIN_PATH override
# them. The images go to firmware/uf2/ and are named openSciLab-pico_<BOARD_TYPE>[_Turbo].uf2,
# which the application uses to recognise the board when flashing.
# Arduino images go to firmware/arduino-images/, the bridge app to firmware/apk/.

set -eo pipefail

FIRMWARE_DIR="$(cd "$(dirname "$0")" && pwd)"
ALL_BOARDS=(BOARD_PICO BOARD_PICO_2 BOARD_PICO_W BOARD_PICO_W_WIFI BOARD_PICO_2_W BOARD_PICO_2_W_WIFI BOARD_ZERO BOARD_INTERCEPTOR)

# ------------------------------------------------------------------ arguments
PICO=0
ARDUINO=0
BRIDGE=0
EXPLICIT=0
BOARDS=()
for argument in "$@"; do
    case "$argument" in
        --pico) PICO=1; EXPLICIT=1 ;;
        --arduino) ARDUINO=1; EXPLICIT=1 ;;
        --bridge) BRIDGE=1; EXPLICIT=1 ;;
        --all) PICO=1; ARDUINO=1; BRIDGE=1 ;;
        -h|--help) sed -n '8,21p' "$0" | sed -e 's/^# //' -e 's/^#$//'; exit 0 ;;
        BOARD_*) PICO=1; EXPLICIT=1; BOARDS+=("$argument") ;;
        *) echo "Unknown argument $argument (see --help)" >&2; exit 1 ;;
    esac
done
if [ "$PICO$ARDUINO$BRIDGE" = 000 ]; then
    PICO=1; ARDUINO=1; BRIDGE=1  # everything there is
fi

# ----------------------------------------------------------------------- Pico
build_pico() {
    SOURCE_DIR="$FIRMWARE_DIR/pico"
    OUTPUT_DIR="$FIRMWARE_DIR/uf2"
    SETTINGS="$SOURCE_DIR/build_settings.cmake"
    # Separate from pico/build, so the VS Code build keeps its board in the cache
    BUILD_DIR="$OUTPUT_DIR/.build"

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

    if [ "${#BOARDS[@]}" = 0 ]; then
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

            name="openSciLab-pico_${board}"
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
                cp "$BUILD_DIR/openscilab_pico.uf2" "$OUTPUT_DIR/$name.uf2"
                rm -f "$log"
                built=$((built + 1))
            else
                echo "    failed, see $log" >&2
                failed=$((failed + 1))
            fi
        done
    done

    rm -rf "$BUILD_DIR"
    mv "$SETTINGS.orig" "$SETTINGS"
    trap - EXIT

    echo
    echo "$built image(s) in $OUTPUT_DIR, $failed failed."
    [ "$failed" = 0 ]
}

# -------------------------------------------------------------------- Arduino
build_arduino() {
    local source="$FIRMWARE_DIR/arduino"
    if [ ! -f "$source/platformio.ini" ]; then
        echo "No Arduino firmware yet (firmware/arduino comes with phase 2)."
        [ "$EXPLICIT" = 0 ]
        return
    fi
    if ! command -v pio >/dev/null 2>&1; then
        echo "PlatformIO (pio) not found: pip install platformio" >&2
        return 1
    fi
    echo "==> Arduino"
    pio run -d "$source"
    mkdir -p "$FIRMWARE_DIR/arduino-images"
    find "$source/.pio/build" -maxdepth 2 \( -name "firmware.hex" -o -name "firmware.bin" \) | while read -r image; do
        environment="$(basename "$(dirname "$image")")"
        cp "$image" "$FIRMWARE_DIR/arduino-images/openSciLab_${environment}.${image##*.}"
    done
    echo "Arduino images in $FIRMWARE_DIR/arduino-images"
}

# --------------------------------------------------------------------- Bridge
build_bridge() {
    local source="$FIRMWARE_DIR/bridge-android"
    if [ ! -f "$source/gradlew" ]; then
        echo "No bridge app yet (firmware/bridge-android comes with phase 3)."
        [ "$EXPLICIT" = 0 ]
        return
    fi
    echo "==> Bridge"
    (cd "$source" && ./gradlew --no-daemon assembleRelease)
    mkdir -p "$FIRMWARE_DIR/apk"
    cp "$source"/app/build/outputs/apk/release/*.apk "$FIRMWARE_DIR/apk/openSciLab-bridge.apk"
    echo "Bridge app in $FIRMWARE_DIR/apk"
}

status=0
if [ "$PICO" = 1 ]; then build_pico || status=1; fi
if [ "$ARDUINO" = 1 ]; then build_arduino || status=1; fi
if [ "$BRIDGE" = 1 ]; then build_bridge || status=1; fi
exit $status
