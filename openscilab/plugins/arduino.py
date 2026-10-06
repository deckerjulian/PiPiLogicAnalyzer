# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Built-in plugin: Arduino boards with the openSciLab Arduino firmware (``driver/arduino``).

``arduino:/dev/cu.usbserial-1410``
    A board on a serial port.
``arduino-sim:uno``
    A simulated Arduino that speaks the protocol of the firmware (``driver/simulated/arduino_shell``).
"""

from __future__ import annotations

from ..driver import kinds
from ..driver.base import AnalyzerDriverBase, DeviceConnectionError


def open_arduino(rest: str) -> AnalyzerDriverBase:
    from ..driver.arduino.driver import open_serial

    if not rest.strip():
        raise DeviceConnectionError("Give the serial port of the board: arduino:<port>.")
    return open_serial(rest.strip())


def open_simulated(rest: str) -> AnalyzerDriverBase:
    from ..driver.simulated.arduino_shell import open_shell

    try:
        return open_shell(rest or "uno")
    except ValueError as error:
        raise DeviceConnectionError(str(error)) from None


def detect() -> list[tuple[str, str]]:
    from ..driver import ports

    return [(port.port_name, f"Arduino on {port.label}") for port in ports.detect_arduinos()]


kinds.register("arduino", open_arduino, title="Arduino", detect=detect, process=True, simulation="uno")
kinds.register("arduino-sim", open_simulated, title="Simulated Arduino (firmware protocol)", simulation="uno")


def setup_ui() -> None:
    from ..ui.devices import register_backend
    from ..ui.devices.arduino import ArduinoBackend

    register_backend(ArduinoBackend())
