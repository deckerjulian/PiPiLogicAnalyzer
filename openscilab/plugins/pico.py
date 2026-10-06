# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Built-in plugin: Pico boards with the openSciLab Pico firmware (``driver/pico``).

``pico:/dev/cu.usbmodem1``
    A board on a serial port (``pico:COM5`` on Windows); an address without a kind is one too.
``pico-net:192.168.1.5:4045``
    A Pico W board over WiFi (``192.168.1.5:4045`` without a kind as well).
``pico-multi:/dev/cu.usbmodem1,/dev/cu.usbmodem2``
    A multi device set (2 to 5 boards, the first one triggers the others; a comma separated list
    without a kind as well).
"""

from __future__ import annotations

import re
import socket

from ..driver import kinds
from ..driver.base import AnalyzerDriverBase, DeviceConnectionError

_HOST_PORT_RE = re.compile(r"^([A-Za-z0-9.\-]+):(\d{1,5})$")
_IPV4_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def open_pico(rest: str) -> AnalyzerDriverBase:
    """A board on a serial port, at ``host:port`` or a list of them."""
    rest = rest.strip()
    if not rest:
        raise DeviceConnectionError("Give the serial port of the board: pico:<port>.")
    if "," in rest:
        return open_multi(rest)
    if _HOST_PORT_RE.match(rest) and not re.match(r"^[A-Za-z]:", rest):
        return _open_board(_network_address(rest))
    return _open_board(rest)


def open_network(rest: str) -> AnalyzerDriverBase:
    return _open_board(_network_address(rest))


def open_multi(rest: str) -> AnalyzerDriverBase:
    from ..driver.pico.multi import MultiAnalyzerDriver

    strings = [_connection_string(item) for item in rest.split(",") if item.strip()]
    try:
        return MultiAnalyzerDriver(strings)
    except ValueError as error:
        raise DeviceConnectionError(str(error)) from error


def detect() -> list[tuple[str, str]]:
    from ..driver.pico import detector

    return [(device.port_name, f"openSciLab Pico on {device.port_name}"
             + (f", S/N {device.serial_number}" if device.serial_number else ""))
            for device in detector.detect()]


def _network_address(text: str) -> str:
    """``address:port`` with the host name resolved (the driver takes IPv4 addresses)."""
    match = _HOST_PORT_RE.match(text.strip())
    if not match:
        raise DeviceConnectionError(f"Invalid network address '{text}': use <address>:<port>.")
    host, port = match.groups()
    if not _IPV4_RE.match(host):
        try:
            host = socket.gethostbyname(host)
        except OSError as error:
            raise DeviceConnectionError(f"The host {host} was not found: {error}") from error
    return f"{host}:{port}"


def _connection_string(item: str) -> str:
    item = item.strip()
    for prefix in ("pico-net:", "pico:"):
        if item.lower().startswith(prefix):
            rest = item[len(prefix):]
            return _network_address(rest) if prefix == "pico-net:" else rest
    if _HOST_PORT_RE.match(item):
        return _network_address(item)
    return item


def _open_board(connection_string: str) -> AnalyzerDriverBase:
    from ..driver.pico.analyzer import PicoDriver

    try:
        return PicoDriver(connection_string)
    except DeviceConnectionError:
        raise
    except (OSError, ValueError) as error:
        raise DeviceConnectionError(f"{connection_string} could not be opened: {error}") from error


kinds.register("pico", open_pico, title="openSciLab Pico", detect=detect, process=True, simulation="pico")
kinds.register("pico-net", open_network, title="openSciLab Pico W (network)", process=True, simulation="pico")
kinds.register("pico-multi", open_multi, title="Multi device set of Pico boards", process=True, simulation="pico")


def setup_ui() -> None:
    from ..ui.devices import register_backend
    from ..ui.devices.pico import PicoBackend

    register_backend(PicoBackend())
