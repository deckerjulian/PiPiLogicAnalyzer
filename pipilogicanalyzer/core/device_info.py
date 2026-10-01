# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Description of a connected analyzer for the *Device information* dialog.

The driver describes its board, firmware and connection (``AnalyzerDriverBase.describe``);
the capture limits are the same for every device.
"""

from __future__ import annotations

from typing import Any, Callable

from ..driver.base import AnalyzerDriverBase, DeviceSection
from .formatting import to_large_frequency, to_thousands

Section = DeviceSection


def _safe(call: Callable[[], Any], default: Any) -> Any:
    try:
        return call()
    except Exception:  # noqa: BLE001 - information only, never fail the dialog
        return default


def describe_device(driver: AnalyzerDriverBase) -> list[Section]:
    sections = _safe(driver.describe, None) or [("Device", [("Identification", driver.device_version or "-")])]
    return sections + [_capture_section(driver)]


def _capture_section(driver: AnalyzerDriverBase) -> Section:
    rows = [
        ("Channels", str(driver.channel_count)),
        ("Buffer size", f"{to_thousands(driver.buffer_size)} bytes"),
        ("Max. frequency", to_large_frequency(driver.max_frequency)),
        ("Min. frequency", to_large_frequency(driver.min_frequency)),
    ]
    if driver.blast_frequency:
        rows.append(("Blast frequency", to_large_frequency(driver.blast_frequency)))
    # A multi device set captures a single burst
    if driver.is_hardware and driver.board_count == 1 and driver.max_loop_count > 0:
        rows.append(("Max. bursts", to_thousands(driver.max_loop_count + 1)))
    modes = _safe(driver.acquisition_modes, ())
    if modes:
        rows.append(("Acquisition", ", ".join(mode.capitalize() for mode in modes)))
    return ("Capture", rows)


def as_text(sections: list[Section]) -> str:
    lines = []
    for title, rows in sections:
        lines.append(f"[{title}]")
        lines += [f"{key}: {value}" for key, value in rows]
        lines.append("")
    return "\n".join(lines).strip() + "\n"
