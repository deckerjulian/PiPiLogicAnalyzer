# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finding and opening devices without the user interface (command line, scripts).

Every device has an address ``<kind>:<rest>`` that :func:`open_device` understands; the kinds are
registered by plugins (:mod:`.kinds`), those of openSciLab by its own (:mod:`openscilab.plugins`):

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
``arduino-sim:uno``
    A simulated Arduino that speaks the protocol of the firmware (``driver/simulated/arduino_shell``).
``rigol:192.168.1.20``
    A Rigol DHO900 oscilloscope over the network (through its bridge app when it runs,
    ``rigol:<address>:<port>`` for another port).
``rigol-sim:bridge``
    A simulated DHO924S with the bridge app (``rigol-sim:direct`` without it).
``<kind>:...``
    A device of a kind a plugin of the user added.

An address without a kind - a serial port, ``host:port`` or a comma separated list of them - is
one of Pico boards. Remote devices (``remote:``, ``remote-sim:``) have no capture driver: they are
opened as instruments (``lab.engine.devices``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import kinds
from .base import AnalyzerDriverBase, DeviceConnectionError

log = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """A device found by :func:`list_devices`."""

    #: Identifier for :func:`open_device`, e.g. ``"pico:/dev/cu.usbmodem1"``
    id: str
    #: Description for people, e.g. ``"openSciLab Pico on /dev/cu.usbmodem1, S/N E6614..."``
    label: str
    #: its kind (``"pico"``)
    kind: str

    def __str__(self) -> str:
        return f"{self.id}  {self.label}"


def list_devices() -> list[DeviceInfo]:
    """The connected devices of every kind that can find its devices (Pico boards and Arduino
    boards on USB, DSLogic analyzers, those of plugins).

    Network boards cannot be discovered; open them with ``pico-net:<address>:<port>``.
    """
    devices: list[DeviceInfo] = []
    for kind in kinds.registered():
        devices += detect_kind(kind)
    return devices


def detect_kind(kind: kinds.DeviceKind) -> list[DeviceInfo]:
    """The connected devices of a kind (none when its ``detect`` fails)."""
    if kind.detect is None:
        return []
    try:
        return [DeviceInfo(id=f"{kind.kind}:{value}", label=label or f"{kind.title} on {value}", kind=kind.kind)
                for value, label in kind.detect()]
    except Exception:  # (one kind of device must not hide the others)
        log.warning("The devices of the kind %s cannot be listed", kind.kind, exc_info=True)
        return []


def open_device(device_id: str, **options: Any) -> AnalyzerDriverBase:
    """Connect to a device (see the module documentation for the addresses); ``options`` go to the
    kind's opener when it takes them.

    Raises :class:`~.base.DeviceConnectionError` when the device cannot be opened; for a DSLogic
    whose FPGA bitstream is missing :class:`~.dslogic.driver.BitstreamMissingError` (a subclass),
    unless ``download_bitstream=True`` allows fetching it from the DSView repository.
    """
    device_id = (device_id or "").strip()
    if not device_id:
        raise DeviceConnectionError("No device given.")
    name, rest = kinds.split(device_id)
    kind = kinds.find(name)
    if kind is None:
        known = ", ".join(entry.kind for entry in kinds.registered())
        raise DeviceConnectionError(f"Unknown kind of device '{name}' in '{device_id}' (known: {known}; "
                                    "a device of a plugin needs its plugin, see docs/drivers.md).")
    if kind.open is None:
        raise DeviceConnectionError(f"{device_id}: a {kind.title} has no capture driver; it is opened as an "
                                    "instrument (a flow, openscilab.api.instrument).")
    return kinds.call(kind.open, rest, **options)
