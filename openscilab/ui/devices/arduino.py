# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Arduino boards with the openSciLab Arduino firmware, and simulated ones that speak its protocol."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...driver import ports
from ...driver.simulated.profiles import available_profiles, load_profile
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "arduino"
#: the protocol shell of the simulator (address ``arduino-sim:<profile>``)
KIND_SIMULATED = "sim"


def arduino_profiles() -> list[str]:
    """Simulator profiles the protocol shell can play: the boards that run the openSciLab Arduino
    firmware (``"firmware": "arduino"`` in the profile)."""
    names = []
    for name in available_profiles():
        try:
            profile = load_profile(name)
        except (ValueError, OSError):
            continue
        if profile.get("firmware") == "arduino":
            names.append(name)
    return names


class ArduinoBackend(DeviceBackend):
    id = BACKEND_ID

    def detected(self) -> list[DeviceEntry]:
        # nothing is opened here: opening a port resets the board, which takes seconds
        return [DeviceEntry(BACKEND_ID, "serial", port.port_name, f"Arduino on {port.label}")
                for port in ports.detect_arduinos()]

    def manual_entries(self) -> list[DeviceEntry]:
        return [DeviceEntry(BACKEND_ID, KIND_SIMULATED, name,
                            f"{load_profile(name)['title']} (firmware protocol)", simulated=True)
                for name in arduino_profiles()]

    def address(self, entry: DeviceEntry, parent: QWidget) -> Optional[str]:
        # a simulated Arduino stays in the application, a board on USB is read in a device process
        return f"arduino-sim:{entry.value}" if entry.kind == KIND_SIMULATED else f"arduino:{entry.value}"
