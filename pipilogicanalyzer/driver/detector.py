# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Detection of connected PiPiLogicAnalyzer devices.

Port of ``SharedDriver/DeviceDetector.cs``.  The original implementation walked
the Windows registry and the Linux sysfs tree separately and did not support
macOS at all.  Here ``pyserial``'s port enumeration is used, which exposes
VID/PID/serial number on every supported platform, with a sysfs fallback for
Linux systems where the USB descriptors are not surfaced by pyserial.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from serial.tools import list_ports

#: USB identifiers of the PiPiLogicAnalyzer firmware.
VID = 0x1209
PID = 0x3020

_SYSFS_DEVICE_RE = re.compile(r"^\d+-\d+$")
_SYSFS_ROOT = "/sys/bus/usb/devices"


@dataclass
class DetectedDevice:
    port_name: str
    serial_number: Optional[str] = None
    vid: int = VID
    pid: int = PID
    device_path: Optional[str] = None
    parent_id: Optional[str] = None

    def __str__(self) -> str:
        if self.serial_number:
            return f"{self.port_name} ({self.serial_number})"
        return self.port_name


def detect() -> list[DetectedDevice]:
    """Return every PiPiLogicAnalyzer device currently connected."""
    devices = _detect_pyserial()
    if not devices:
        devices = _detect_linux_sysfs()
    devices.sort(key=lambda device: device.port_name)
    return devices


def _detect_pyserial() -> list[DetectedDevice]:
    devices: list[DetectedDevice] = []
    for port in list_ports.comports():
        if port.vid != VID or port.pid != PID:
            continue
        devices.append(
            DetectedDevice(
                port_name=port.device,
                serial_number=port.serial_number,
                vid=port.vid,
                pid=port.pid,
                device_path=port.location or port.hwid,
                parent_id=port.location,
            )
        )
    return devices


def _detect_linux_sysfs() -> list[DetectedDevice]:
    if not os.path.isdir(_SYSFS_ROOT):
        return []

    devices: list[DetectedDevice] = []
    for entry in sorted(os.listdir(_SYSFS_ROOT)):
        if not _SYSFS_DEVICE_RE.match(entry):
            continue
        directory = os.path.join(_SYSFS_ROOT, entry)
        try:
            id_vendor = _read_sysfs(directory, "idVendor")
            id_product = _read_sysfs(directory, "idProduct")
            if int(id_vendor, 16) != VID or int(id_product, 16) != PID:
                continue
            serial_number = _read_sysfs(directory, "serial")
        except (OSError, ValueError):
            continue

        tty_directory = f"{directory}:1.0/tty"
        if not os.path.isdir(tty_directory):
            continue

        for tty in sorted(os.listdir(tty_directory)):
            devices.append(
                DetectedDevice(
                    port_name=f"/dev/{tty}",
                    serial_number=serial_number,
                    device_path=f"{directory}:1.0",
                    parent_id=entry,
                )
            )
    return devices


def _read_sysfs(directory: str, name: str) -> str:
    with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
        return handle.read().strip()


#: USB vendor of Raspberry Pi; boards running other firmware (MicroPython, Pico SDK programs) use it.
RASPBERRY_PI_VID = 0x2E8A
#: Product names of well known Raspberry Pi USB product IDs.
RASPBERRY_PI_PRODUCTS = {0x0005: "MicroPython", 0x000A: "Pico SDK program"}


@dataclass
class ForeignPico:
    """A Raspberry Pi board with a USB serial port that is not running this firmware."""

    port_name: str
    pid: int
    description: str
    serial_number: Optional[str] = None


@dataclass
class UsbPortInfo:
    port_name: str
    vid: Optional[int]
    pid: Optional[int]
    manufacturer: Optional[str]
    product: Optional[str]
    serial_number: Optional[str]
    location: Optional[str]
    description: Optional[str]

    @property
    def is_analyzer(self) -> bool:
        return self.vid == VID and self.pid == PID

    @property
    def device_name(self) -> str:
        """What is behind the port, as far as the USB descriptors tell."""
        if self.is_analyzer:
            return "PiPiLogicAnalyzer"
        if self.vid == RASPBERRY_PI_VID:
            return RASPBERRY_PI_PRODUCTS.get(self.pid) or self.product or "Raspberry Pi board"
        # pyserial reports "n/a" for ports without USB descriptors
        if self.description and self.description != "n/a" and self.description != self.port_name:
            return self.product or self.description
        return self.product or ""

    @property
    def label(self) -> str:
        """Port name with device and serial number, to tell identical boards apart."""
        extras = [self.device_name] if self.device_name else []
        if self.serial_number:
            extras.append(f"S/N {self.serial_number}")
        return f"{self.port_name} ({', '.join(extras)})" if extras else self.port_name


def _usb_info(port) -> UsbPortInfo:
    return UsbPortInfo(
        port_name=port.device,
        vid=port.vid,
        pid=port.pid,
        manufacturer=port.manufacturer,
        product=port.product,
        serial_number=port.serial_number,
        location=port.location,
        description=port.description,
    )


def detect_foreign_picos() -> list[ForeignPico]:
    boards = []
    for port in list_ports.comports():
        if port.vid != RASPBERRY_PI_VID:
            continue
        description = RASPBERRY_PI_PRODUCTS.get(port.pid) or port.product or port.description or "other firmware"
        boards.append(
            ForeignPico(
                port_name=port.device,
                pid=port.pid or 0,
                description=description,
                serial_number=port.serial_number,
            )
        )
    boards.sort(key=lambda board: board.port_name)
    return boards


def port_details(port_name: str) -> Optional[UsbPortInfo]:
    """USB descriptor information of a serial port, if the system provides it."""
    for port in list_ports.comports():
        if port.device == port_name:
            return _usb_info(port)
    return None


def list_port_infos() -> list[UsbPortInfo]:
    """USB descriptor information of every serial port on the machine."""
    return sorted((_usb_info(port) for port in list_ports.comports()), key=lambda info: info.port_name)


def list_serial_ports() -> list[str]:
    """Every serial port on the machine, detected devices included."""
    return sorted(port.device for port in list_ports.comports())
