# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Firmware images, Raspberry Pi Pico bootloader drives and flashing.

A Pico in bootloader mode (BOOTSEL held while plugging it in, or restarted by
software) appears as a USB drive named ``RPI-RP2`` (RP2040) or ``RP2350`` that
contains ``INFO_UF2.TXT``. Copying a UF2 image onto that drive flashes it and
the board restarts with the new firmware.
"""

from __future__ import annotations

import os
import re
import struct
import sys
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .settings import settings_directory

PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------- boards
@dataclass(frozen=True)
class BoardModel:
    #: Name reported by the firmware (``BOARD_NAME``)
    name: str
    #: ``BOARD_TYPE`` of ``PiPiLogicAnalyzer_Build_Settings.cmake``
    board_type: str
    label: str
    chip: str
    wifi: bool = False


BOARDS = (
    BoardModel("PICO", "BOARD_PICO", "Raspberry Pi Pico", "RP2040"),
    BoardModel("PICO_2", "BOARD_PICO_2", "Raspberry Pi Pico 2", "RP2350"),
    BoardModel("W", "BOARD_PICO_W", "Raspberry Pi Pico W (data over USB)", "RP2040"),
    BoardModel("WIFI", "BOARD_PICO_W_WIFI", "Raspberry Pi Pico W (WiFi)", "RP2040", wifi=True),
    BoardModel("2_W", "BOARD_PICO_2_W", "Raspberry Pi Pico 2 W (data over USB)", "RP2350"),
    BoardModel("2_WIFI", "BOARD_PICO_2_W_WIFI", "Raspberry Pi Pico 2 W (WiFi)", "RP2350", wifi=True),
    BoardModel("ZERO", "BOARD_ZERO", "RP2040-Zero", "RP2040"),
    BoardModel("INTERCEPTOR", "BOARD_INTERCEPTOR", "LogicAnalyzer Interceptor", "RP2040"),
)


def board_by_name(name: str) -> Optional[BoardModel]:
    return next((board for board in BOARDS if board.name == name), None)


def board_from_text(text: str) -> Optional[BoardModel]:
    """Board whose ``BOARD_TYPE`` appears in ``text`` (e.g. a file name)."""
    upper = text.upper()
    for board in sorted(BOARDS, key=lambda item: len(item.board_type), reverse=True):
        if board.board_type in upper:
            return board
    return None


@dataclass(frozen=True)
class FirmwareIdentity:
    raw: str
    board_name: str
    major: int
    minor: int

    @property
    def board(self) -> Optional[BoardModel]:
        return board_by_name(self.board_name)


_VERSION_RE = re.compile(r"^(?:PIPI_)?LOGIC_ANALYZER_(?:(?P<board>.+)_)?V(?P<major>\d+)_(?P<minor>\d+)$")


def parse_device_version(version: Optional[str]) -> Optional[FirmwareIdentity]:
    """Split ``[PIPI_]LOGIC_ANALYZER_<BOARD>_V<major>_<minor>`` (older firmware has no prefix)."""
    match = _VERSION_RE.match((version or "").strip())
    if not match:
        return None
    return FirmwareIdentity(
        raw=version or "",
        board_name=match.group("board") or "",
        major=int(match.group("major")),
        minor=int(match.group("minor")),
    )


def describe_firmware(version: Optional[str], details: Optional[dict] = None) -> str:
    """Installed firmware and board of an analyzer in one line.

    ``details`` are the ``INFO`` lines of firmware with device details; older
    firmware only reports the identification string.
    """
    details = details or {}
    identity = parse_device_version(version)
    if identity is None:
        return f"firmware {version}" if version else "firmware version unknown"

    board = identity.board
    if board is not None:
        board_label = board.label
    elif identity.board_name:
        board_label = identity.board_name
    else:
        # V6_0 and older do not report the board in the identification.
        board_label = details.get("BOARD") or "board not reported"
    parts = [f"firmware {identity.major}.{identity.minor}", board_label]
    chip = details.get("CHIP") or (board.chip if board else "")
    if chip:
        parts.append(chip)
    if details.get("SDK"):
        parts.append(f"Pico SDK {details['SDK']}")
    return " · ".join(parts)


# ------------------------------------------------------------------------- UF2
UF2_BLOCK_SIZE = 512
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FAMILY_ID = 0x00002000

FAMILY_RP2040 = 0xE48BFF56
FAMILY_ABSOLUTE = 0xE48BFF57
FAMILY_DATA = 0xE48BFF58
FAMILY_RP2350_ARM_S = 0xE48BFF59
FAMILY_RP2350_RISCV = 0xE48BFF5A
FAMILY_RP2350_ARM_NS = 0xE48BFF5B

CHIP_OF_FAMILY = {
    FAMILY_RP2040: "RP2040",
    FAMILY_RP2350_ARM_S: "RP2350",
    FAMILY_RP2350_RISCV: "RP2350",
    FAMILY_RP2350_ARM_NS: "RP2350",
}


#: Version string the firmware answers with, as it is stored in the image.
#: The lookahead keeps a version like ``V7_10`` from being read as ``V7_1``.
_IMAGE_VERSION_RE = re.compile(rb"(?:PIPI_)?LOGIC_ANALYZER_[A-Z0-9_]*?V[0-9]+_[0-9]+(?![0-9])")


@dataclass(frozen=True)
class Uf2Image:
    path: str
    families: frozenset
    block_count: int
    data_size: int
    board: Optional[BoardModel]
    turbo: bool
    #: ``PIPI_LOGIC_ANALYZER_<BOARD>_V<major>_<minor>`` found in the image (``None`` for other firmware).
    device_version: Optional[str] = None

    @property
    def file_name(self) -> str:
        return os.path.basename(self.path)

    @property
    def chip(self) -> Optional[str]:
        chips = {CHIP_OF_FAMILY[family] for family in self.families if family in CHIP_OF_FAMILY}
        return chips.pop() if len(chips) == 1 else None

    @property
    def version(self) -> Optional[str]:
        """Firmware version of the image as ``<major>.<minor>``."""
        identity = parse_device_version(self.device_version)
        return f"{identity.major}.{identity.minor}" if identity else None

    @property
    def version_label(self) -> str:
        return f"firmware {self.version}" if self.version else "unknown firmware version"

    @property
    def label(self) -> str:
        board = self.board.label if self.board else "Unknown board"
        version = f"{self.version} " if self.version else ""
        return f"{board}{' (Turbo)' if self.turbo else ''} - {version}{self.file_name}"


def read_uf2(path: str) -> Uf2Image:
    """Validate a UF2 image and read its chip families (raises ``OSError``/``ValueError``)."""
    with open(path, "rb") as handle:
        data = handle.read()
    if not data or len(data) % UF2_BLOCK_SIZE:
        raise ValueError(f"{os.path.basename(path)} is not a UF2 image")

    families = set()
    data_size = 0
    flashed = bytearray()
    for offset in range(0, len(data), UF2_BLOCK_SIZE):
        magic0, magic1, flags, _address, payload, _number, _count, family = struct.unpack_from(
            "<IIIIIIII", data, offset
        )
        (magic_end,) = struct.unpack_from("<I", data, offset + UF2_BLOCK_SIZE - 4)
        if magic0 != UF2_MAGIC_START0 or magic1 != UF2_MAGIC_START1 or magic_end != UF2_MAGIC_END:
            raise ValueError(f"{os.path.basename(path)} is not a UF2 image")
        if flags & UF2_FLAG_FAMILY_ID:
            families.add(family)
        data_size += payload
        # The version string may straddle two blocks: keep the payload in order.
        flashed += data[offset + 32 : offset + 32 + min(payload, UF2_BLOCK_SIZE - 32 - 4)]

    match = _IMAGE_VERSION_RE.search(bytes(flashed))
    name = os.path.basename(path)
    return Uf2Image(
        path=os.path.abspath(path),
        families=frozenset(families),
        block_count=len(data) // UF2_BLOCK_SIZE,
        data_size=data_size,
        board=board_from_text(name),
        turbo="TURBO" in name.upper(),
        device_version=match.group().decode("ascii") if match else None,
    )


def image_directories() -> list[str]:
    """Where firmware images are looked for (``firmware/build_all.sh`` writes to the first)."""
    return [
        os.path.join(PROJECT_DIRECTORY, "firmware", "uf2"),
        os.path.join(PROJECT_DIRECTORY, "firmware", "PiPiLogicAnalyzer", "build"),
        os.path.join(settings_directory(), "firmware"),
    ]


def find_images(directories: Optional[Sequence[str]] = None) -> list[Uf2Image]:
    images = []
    for directory in directories if directories is not None else image_directories():
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".uf2"):
                continue
            try:
                images.append(read_uf2(os.path.join(directory, name)))
            except (OSError, ValueError):
                continue
    return images


def is_compatible(image: Uf2Image, chip: Optional[str]) -> Optional[bool]:
    """``True``/``False`` when image and chip are known, ``None`` otherwise."""
    if image.chip is None or not chip:
        return None
    return image.chip == chip


# ---------------------------------------------------------- bootloader drives
@dataclass(frozen=True)
class BootDrive:
    path: str
    chip: str
    info: tuple  # (key, value) pairs of INFO_UF2.TXT

    @property
    def name(self) -> str:
        return os.path.basename(os.path.normpath(self.path)) or self.path

    @property
    def bootloader(self) -> str:
        return dict(self.info).get("UF2 Bootloader", "")


def parse_info_uf2(text: str) -> list[tuple[str, str]]:
    entries = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            entries.append((key.strip(), value.strip()))
        elif line.strip():
            # First line: "UF2 Bootloader v3.0"
            head, _, version = line.strip().rpartition(" ")
            entries.append((head or line.strip(), version))
    return entries


def boot_drive_at(path: str) -> Optional[BootDrive]:
    try:
        names = os.listdir(path)
    except OSError:
        return None
    info_name = next((name for name in names if name.upper() == "INFO_UF2.TXT"), None)
    if info_name is None:
        return None
    try:
        with open(os.path.join(path, info_name), "r", encoding="ascii", errors="replace") as handle:
            info = parse_info_uf2(handle.read())
    except OSError:
        return None

    board_id = dict(info).get("Board-ID", "").upper()
    if "RP2350" in board_id:
        chip = "RP2350"
    elif "RPI-RP2" in board_id or "RP2040" in board_id:
        chip = "RP2040"
    else:
        return None  # a UF2 bootloader of another microcontroller family
    return BootDrive(path=path, chip=chip, info=tuple(info))


def candidate_mount_points() -> list[str]:
    candidates: list[str] = []

    def children(directory: str) -> Iterable[str]:
        try:
            return [os.path.join(directory, name) for name in sorted(os.listdir(directory))]
        except OSError:
            return []

    if sys.platform == "darwin":
        candidates += children("/Volumes")
    elif sys.platform.startswith("win"):
        candidates += [f"{letter}:\\" for letter in "DEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")]
    else:
        for root in ("/media", "/run/media"):
            for entry in children(root):
                candidates.append(entry)
                candidates += children(entry)
        candidates += children("/mnt")
    return candidates


def find_boot_drives(mount_points: Optional[Sequence[str]] = None) -> list[BootDrive]:
    drives = []
    for path in mount_points if mount_points is not None else candidate_mount_points():
        drive = boot_drive_at(path)
        if drive is not None:
            drives.append(drive)
    return drives


# -------------------------------------------------------------------- flashing
def flash_image(image_path: str, drive: BootDrive) -> str:
    """Copy ``image_path`` onto the bootloader drive; returns the written file."""
    target = os.path.join(drive.path, "FIRMWARE.UF2")
    size = os.path.getsize(image_path)
    written = 0
    try:
        with open(image_path, "rb") as source, open(target, "wb") as destination:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
                written += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError:
        # The board restarts as soon as it received the last block, so closing the
        # file on the vanished drive may fail although the image was transferred.
        if written < size:
            raise
    return target


def reboot_via_serial_touch(port: str) -> bool:
    """Restart a Pico into its bootloader by opening its USB serial port at 1200 baud.

    Supported by firmware built with the Pico SDK USB stdio (this firmware
    included) and by MicroPython/CircuitPython.
    """
    try:
        import serial
    except ImportError:
        return False

    try:
        connection = serial.Serial(port, 1200)
    except (OSError, ValueError, serial.SerialException):
        return False

    try:
        connection.dtr = False
    except (OSError, serial.SerialException):
        pass
    try:
        connection.close()
    except (OSError, serial.SerialException):
        pass  # the board may already have disconnected to restart
    return True
