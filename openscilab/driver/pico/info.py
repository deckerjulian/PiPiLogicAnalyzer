# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Description of a board with the openSciLab Pico firmware for the *Device information* dialog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ...core.firmware import parse_device_version
from ...core.formatting import to_large_frequency, to_thousands
from ..base import (
    CAPABILITY_DEVICE_INFO,
    CAPABILITY_SELF_TEST,
    CAPABILITY_SIMULATION,
    DeviceSection,
)
from . import detector

if TYPE_CHECKING:
    from .analyzer import PicoDriver

CAPABILITY_LABELS = {
    CAPABILITY_SELF_TEST: "Board self-test",
    CAPABILITY_SIMULATION: "Simulated capture",
    CAPABILITY_DEVICE_INFO: "Device details",
}

DETAIL_LABELS = (
    ("BOARD", "Board name"),
    ("FIRMWARE", "Firmware"),
    ("BUILD_DATE", "Build date"),
    ("SDK", "Pico SDK"),
    ("CHIP", "Microcontroller"),
    ("CHIP_REVISION", "Chip revision"),
    ("ROM_VERSION", "Boot ROM version"),
    ("UNIQUE_ID", "Unique board ID"),
    ("FLASH_SIZE", "Flash size"),
    ("CLOCK", "System clock"),
    ("TURBO", "Turbo mode (overclocked)"),
    ("WIFI", "WiFi"),
    ("PATTERN_TRIGGER", "Pattern trigger"),
)


def _safe(call: Callable[[], Any], default: Any) -> Any:
    try:
        return call()
    except Exception:  # noqa: BLE001 - information only, never fail the dialog
        return default


def describe_board(driver: "PicoDriver") -> list[DeviceSection]:
    version = driver.device_version or ""
    identity = parse_device_version(version)
    capabilities = _safe(driver.capabilities, frozenset())
    details = _safe(driver.device_details, {}) if CAPABILITY_DEVICE_INFO in capabilities else {}

    rows = [("Identification", version or "-")]
    board = identity.board if identity else None
    if identity:
        rows.append(("Board", board.label if board else (identity.board_name or "Unknown")))
        if board:
            rows.append(("Build setting", board.board_type))
        rows.append(("Firmware version", f"{identity.major}.{identity.minor}"))

    chip = details.get("CHIP") or (board.chip if board else "")
    if chip:
        rows.append(("Microcontroller", chip))

    layout = driver.request_layout
    rows.append(
        (
            "Capture protocol",
            f"{layout.size} byte request, {layout.channels} channel entries, "
            f"up to {to_thousands(layout.max_loop_count + 1)} bursts",
        )
    )

    if driver.protocol_version is not None:
        rows.append(("Firmware protocol", str(driver.protocol_version)))
    if capabilities:
        rows.append(
            ("Functions", ", ".join(CAPABILITY_LABELS.get(item, item) for item in sorted(capabilities)))
        )

    sections: list[DeviceSection] = [("Device", rows)]
    if details:
        sections.append(("Firmware build", detail_rows(details)))
    sections.append(("Connection", _connection_rows(driver)))
    return sections


def detail_rows(details: dict[str, str]) -> list[tuple[str, str]]:
    rows = []
    for key, label in DETAIL_LABELS:
        if key not in details:
            continue
        value = details[key]
        if key == "FLASH_SIZE" and value.isdigit():
            value = f"{int(value) / (1024 * 1024):g} MB ({to_thousands(int(value))} bytes)"
        elif key == "CLOCK" and value.isdigit():
            value = to_large_frequency(int(value))
        elif key in ("TURBO", "WIFI", "PATTERN_TRIGGER"):
            value = "Yes" if value == "1" else "No"
        rows.append((label, value))
    known = {key for key, _ in DETAIL_LABELS}
    rows += [(key, value) for key, value in details.items() if key not in known]
    return rows


def _connection_rows(driver: "PicoDriver") -> list[tuple[str, str]]:
    rows = [("Type", driver.driver_type.value)]
    connection = driver.connection_string

    if not driver.is_network:
        rows.append(("Port", connection))
        usb = _safe(lambda: detector.port_details(connection), None)
        if usb is not None:
            if usb.vid is not None and usb.pid is not None:
                rows.append(("USB vendor:product", f"{usb.vid:04X}:{usb.pid:04X}"))
            for label, value in (
                ("Manufacturer", usb.manufacturer),
                ("Product", usb.product),
                ("Serial number", usb.serial_number),
                ("USB location", usb.location),
            ):
                if value:
                    rows.append((label, str(value)))
    else:
        rows.append(("Address", connection))
        status = _safe(driver.get_voltage_status, None)
        if status and "_" in status:
            voltage, _, external = status.partition("_")
            rows.append(("Power", f"{voltage} V, {'USB/external power' if external == '1' else 'battery'}"))
    return rows
