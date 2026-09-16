# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Description of a connected analyzer for the *Device information* dialog."""

from __future__ import annotations

from typing import Any, Callable

from ..driver import detector
from ..driver.base import (
    CAPABILITY_DEVICE_INFO,
    CAPABILITY_SELF_TEST,
    CAPABILITY_SIMULATION,
    AnalyzerDriverBase,
    AnalyzerDriverType,
)
from .firmware import parse_device_version
from .formatting import to_large_frequency, to_thousands

Section = tuple[str, list[tuple[str, str]]]

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

REAL_DEVICES = (AnalyzerDriverType.SERIAL, AnalyzerDriverType.NETWORK)


def _safe(call: Callable[[], Any], default: Any) -> Any:
    try:
        return call()
    except Exception:  # noqa: BLE001 - information only, never fail the dialog
        return default


def describe_device(driver: AnalyzerDriverBase) -> list[Section]:
    if driver.driver_type == AnalyzerDriverType.MULTI:
        devices = list(getattr(driver, "devices", []))
        sections: list[Section] = [
            ("Multi device analyzer", [("Devices", str(len(devices))), ("Identification", driver.device_version or "-")])
        ]
        for number, device in enumerate(devices, start=1):
            sections += [(f"Device {number}: {title}", rows) for title, rows in _describe_single(device)]
        return sections + [_capture_section(driver)]
    return _describe_single(driver) + [_capture_section(driver)]


def _describe_single(driver: AnalyzerDriverBase) -> list[Section]:
    version = driver.device_version or ""
    identity = parse_device_version(version)
    real = driver.driver_type in REAL_DEVICES
    capabilities = _safe(driver.capabilities, frozenset()) if real else frozenset()
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

    layout = getattr(driver, "request_layout", None)
    if layout is not None:
        rows.append(
            (
                "Capture protocol",
                f"{layout.size} byte request, {layout.channels} channel entries, "
                f"up to {to_thousands(layout.max_loop_count + 1)} bursts",
            )
        )

    if real:
        if capabilities:
            rows.append(("Firmware type", "Firmware of this project (extended)"))
            rows.append(
                ("Additional functions", ", ".join(CAPABILITY_LABELS.get(item, item) for item in sorted(capabilities)))
            )
        else:
            rows.append(("Firmware type", "Original or older firmware (no self-test, simulation or details)"))

    sections: list[Section] = [("Device", rows)]
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


def _connection_rows(driver: AnalyzerDriverBase) -> list[tuple[str, str]]:
    rows = [("Type", driver.driver_type.value)]
    connection = getattr(driver, "connection_string", None)

    if driver.driver_type == AnalyzerDriverType.SERIAL and connection:
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
    elif driver.driver_type == AnalyzerDriverType.NETWORK and connection:
        rows.append(("Address", connection))
        status = _safe(driver.get_voltage_status, None)
        if status and "_" in status:
            voltage, _, external = status.partition("_")
            rows.append(("Power", f"{voltage} V, {'USB/external power' if external == '1' else 'battery'}"))
    elif driver.driver_type == AnalyzerDriverType.EMULATED:
        rows.append(("Note", "No hardware: loaded, created or computed captures"))
    return rows


def _capture_section(driver: AnalyzerDriverBase) -> Section:
    rows = [
        ("Channels", str(driver.channel_count)),
        ("Buffer size", f"{to_thousands(driver.buffer_size)} bytes"),
        ("Max. frequency", to_large_frequency(driver.max_frequency)),
        ("Min. frequency", to_large_frequency(driver.min_frequency)),
    ]
    if driver.blast_frequency:
        rows.append(("Blast frequency", to_large_frequency(driver.blast_frequency)))
    if driver.driver_type in REAL_DEVICES:
        rows.append(("Max. bursts", to_thousands(driver.max_loop_count + 1)))
    return ("Capture", rows)


def as_text(sections: list[Section]) -> str:
    lines = []
    for title, rows in sections:
        lines.append(f"[{title}]")
        lines += [f"{key}: {value}" for key, value in rows]
        lines.append("")
    return "\n".join(lines).strip() + "\n"
