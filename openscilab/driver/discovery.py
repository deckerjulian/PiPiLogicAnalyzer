# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding and opening devices without the user interface (command line, scripts).

Every device has an identifier that :func:`open_device` understands:

``pico:/dev/cu.usbmodem1``
    A Pico board with the openSciLab Pico firmware on a serial port (``pico:COM5`` on Windows).
``pico-net:192.168.1.5:4045``
    A Pico W board over WiFi.
``pico-multi:/dev/cu.usbmodem1,/dev/cu.usbmodem2``
    A multi device set (2 to 5 boards, the first one triggers the others).
``dslogic:1:4``
    A DreamSourceLab DSLogic at USB bus 1, address 4 (``dslogic`` alone: the first one).
``emulated``
    No hardware: the test signals of the simulation (trigger type ``SIMULATION``).
``sim:free``
    A simulated instrument of a simulator profile (``sim:<profile>``, see ``driver/simulated``).
``arduino:/dev/cu.usbserial-1410``
    An Arduino board with the openSciLab Arduino firmware on a serial port.
``rigol:192.168.1.20``
    A Rigol DHO900 oscilloscope over the network (through its bridge app when it runs,
    ``rigol:<address>:<port>`` for another port).
``rigol-sim:bridge``
    A simulated DHO924S with the bridge app (``rigol-sim:direct`` without it).
``arduino-sim:uno``
    A simulated Arduino that speaks the protocol of the firmware (``driver/simulated/arduino_shell``).

``<kind>:...``
    A device of a kind a plugin added (:mod:`.kinds`).

Plain serial port names, ``host:port`` and comma separated lists of ports are accepted as well.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from typing import Optional

from . import kinds
from .base import AnalyzerDriverBase, DeviceConnectionError

KIND_PICO = "pico"
KIND_PICO_NETWORK = "pico-net"
KIND_PICO_MULTI = "pico-multi"
KIND_DSLOGIC = "dslogic"
KIND_EMULATED = "emulated"
KIND_SIMULATED = "sim"
KIND_ARDUINO = "arduino"
KIND_ARDUINO_SIM = "arduino-sim"
KIND_RIGOL = "rigol"
KIND_RIGOL_SIM = "rigol-sim"

log = logging.getLogger(__name__)

_HOST_PORT_RE = re.compile(r"^([A-Za-z0-9.\-]+):(\d{1,5})$")
_IPV4_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


@dataclass
class DeviceInfo:
    """A device found by :func:`list_devices`."""

    #: Identifier for :func:`open_device`, e.g. ``"pico:/dev/cu.usbmodem1"``
    id: str
    #: Description for people, e.g. ``"openSciLab Pico on /dev/cu.usbmodem1, S/N E6614..."``
    label: str
    #: ``KIND_*``
    kind: str

    def __str__(self) -> str:
        return f"{self.id}  {self.label}"


def list_devices() -> list[DeviceInfo]:
    """The connected Pico boards (USB), Arduino boards (by their USB identifiers) and DSLogic analyzers.

    Network boards cannot be discovered; open them with ``pico-net:<address>:<port>``.
    """
    from .dslogic import usb as dslogic_usb
    from .pico import detector

    devices: list[DeviceInfo] = []
    for device in detector.detect():
        serial = f", S/N {device.serial_number}" if device.serial_number else ""
        devices.append(
            DeviceInfo(
                id=f"{KIND_PICO}:{device.port_name}",
                label=f"openSciLab Pico on {device.port_name}{serial}",
                kind=KIND_PICO,
            )
        )
    from . import ports

    for port in ports.detect_arduinos():
        devices.append(DeviceInfo(id=f"{KIND_ARDUINO}:{port.port_name}", label=f"Arduino on {port.label}",
                                  kind=KIND_ARDUINO))
    for info in dslogic_usb.list_devices():
        devices.append(DeviceInfo(id=f"{KIND_DSLOGIC}:{info.location}", label=info.description, kind=KIND_DSLOGIC))
    for kind in kinds.registered():
        devices += detect_kind(kind)
    return devices


def detect_kind(kind: kinds.DeviceKind) -> list[DeviceInfo]:
    """The connected devices of a kind a plugin added (none when its ``detect`` fails)."""
    if kind.detect is None:
        return []
    try:
        return [DeviceInfo(id=f"{kind.kind}:{value}", label=label or f"{kind.title} on {value}", kind=kind.kind)
                for value, label in kind.detect()]
    except Exception:  # (one kind of device must not hide the others)
        log.warning("The devices of the kind %s cannot be listed", kind.kind, exc_info=True)
        return []


def open_device(device_id: str, download_bitstream: bool = False) -> AnalyzerDriverBase:
    """Connect to a device (see the module documentation for the identifiers).

    Raises :class:`~.base.DeviceConnectionError` when the device cannot be opened; for a DSLogic
    whose FPGA bitstream is missing :class:`~.dslogic.driver.BitstreamMissingError` (a subclass),
    unless ``download_bitstream`` allows fetching it from the DSView repository.
    """
    device_id = (device_id or "").strip()
    if not device_id:
        raise DeviceConnectionError("No device given.")
    kind, _, rest = device_id.partition(":")
    kind = kind.lower()

    if kind == KIND_EMULATED and not rest:
        from .emulated import EmulatedAnalyzerDriver

        return EmulatedAnalyzerDriver()
    if kind == KIND_SIMULATED:
        from .simulated import open_simulated

        try:
            return open_simulated(rest or "free").simulated_driver
        except ValueError as error:
            raise DeviceConnectionError(str(error)) from None
    if kind == KIND_ARDUINO_SIM:
        from .simulated.arduino_shell import open_shell

        try:
            return open_shell(rest or "uno")
        except ValueError as error:
            raise DeviceConnectionError(str(error)) from None
    if kind == KIND_ARDUINO and rest:
        from .arduino.driver import open_serial

        return open_serial(rest)
    if kind == KIND_RIGOL_SIM:
        from .simulated.scpi_shell import open_shell as open_dho_shell

        try:
            return open_dho_shell(rest or "bridge")
        except ValueError as error:
            raise DeviceConnectionError(str(error)) from None
    if kind == KIND_RIGOL and rest:
        from .rigoldho.driver import open_network

        return open_network(rest)
    if kind == KIND_DSLOGIC:
        return _open_dslogic(rest, download_bitstream)
    if kind == KIND_PICO_NETWORK:
        return _open_pico(_network_address(rest))
    if kind == KIND_PICO_MULTI:
        return _open_multi(rest)
    if kind == KIND_PICO and rest:
        if "," in rest:
            return _open_multi(rest)
        return _open_pico(rest)
    added = kinds.find(kind) if rest and kind not in kinds.BUILT_IN else None
    if added is not None:  # (also before host:port: counter:8)
        return added.open(rest)
    if kinds.looks_like_kind(device_id):
        known = ", ".join(sorted(kinds.BUILT_IN | {entry.kind for entry in kinds.registered()}))
        raise DeviceConnectionError(f"Unknown kind of device '{kind}' in '{device_id}' (known: {known}; "
                                    "a device of a plugin needs its plugin, see docs/drivers.md).")
    if "," in device_id:
        return _open_multi(device_id)
    if _HOST_PORT_RE.match(device_id) and not re.match(r"^[A-Za-z]:", device_id):
        return _open_pico(_network_address(device_id))
    return _open_pico(device_id)


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
    for prefix in (f"{KIND_PICO_NETWORK}:", f"{KIND_PICO}:"):
        if item.lower().startswith(prefix):
            rest = item[len(prefix):]
            return _network_address(rest) if prefix.startswith(KIND_PICO_NETWORK) else rest
    if _HOST_PORT_RE.match(item):
        return _network_address(item)
    return item


def _open_pico(connection_string: str) -> AnalyzerDriverBase:
    from .pico.analyzer import PicoDriver

    try:
        return PicoDriver(connection_string)
    except DeviceConnectionError:
        raise
    except (OSError, ValueError) as error:
        raise DeviceConnectionError(f"{connection_string} could not be opened: {error}") from error


def _open_multi(text: str) -> AnalyzerDriverBase:
    from .pico.multi import MultiAnalyzerDriver

    strings = [_connection_string(item) for item in text.split(",") if item.strip()]
    try:
        return MultiAnalyzerDriver(strings)
    except ValueError as error:
        raise DeviceConnectionError(str(error)) from error


def _open_dslogic(location: str, download_bitstream: bool) -> AnalyzerDriverBase:
    from .dslogic import driver as dslogic_driver
    from .dslogic import resources as dslogic_resources
    from .dslogic import usb as dslogic_usb

    info: Optional[dslogic_usb.UsbDeviceInfo]
    if location:
        info = dslogic_usb.find_device(location)
        if info is None:
            raise DeviceConnectionError(f"No DSLogic at USB {location}.")
    else:
        found = dslogic_usb.list_devices()
        if not found:
            raise DeviceConnectionError("No DSLogic is connected.")
        info = found[0]

    info = dslogic_driver.prepare(info)
    try:
        return dslogic_driver.DSLogicDriver(info)
    except dslogic_driver.BitstreamMissingError as missing:
        if not download_bitstream or not dslogic_resources.downloadable(missing.name):
            raise
        try:
            dslogic_resources.download(missing.name)
        except dslogic_resources.ResourceError as error:
            raise DeviceConnectionError(f"The bitstream could not be downloaded: {error}") from error
        return dslogic_driver.DSLogicDriver(info)
