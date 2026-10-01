# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pico boards with the PiPiLogicAnalyzer firmware: over USB, the network, or as a multi device set."""

from __future__ import annotations

import html
from typing import Optional

from PySide6.QtWidgets import QWidget

from ...core import firmware as firmware_images
from ...core import settings
from ...driver.base import AnalyzerDriverBase
from ...driver.pico import detector
from ...driver.pico.analyzer import PiPiLogicAnalyzerDriver
from ...driver.pico.multi import MultiAnalyzerDriver
from .. import messages
from ..dialogs.device_dialogs import MultiComposeDialog, MultiConnectDialog, NetworkConnectDialog
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "pico"
KNOWN_DEVICES_FILE = "known-devices.json"


def serial_entry(port: str, label: str = "") -> DeviceEntry:
    return DeviceEntry(BACKEND_ID, "serial", port, label)


def known_device_sets() -> list[dict]:
    """The stored multi device sets (serial numbers of their boards, in order)."""
    return settings.get_settings(KNOWN_DEVICES_FILE) or []


def forget_known_device_sets() -> None:
    settings.persist_settings(KNOWN_DEVICES_FILE, [])


def _known_device_order(devices) -> Optional[list[str]]:
    serials = {device.serial_number for device in devices if device.serial_number}
    for entry in known_device_sets():
        stored = entry.get("serial_numbers") or []
        if set(stored) == serials and len(stored) == len(devices):
            by_serial = {device.serial_number: device for device in devices}
            return [by_serial[serial].port_name for serial in stored]
    return None


def _store_known_device(devices) -> None:
    known_devices = known_device_sets()
    known_devices.append({"serial_numbers": [device.serial_number for device in devices]})
    settings.persist_settings(KNOWN_DEVICES_FILE, known_devices)


class PicoBackend(DeviceBackend):
    id = BACKEND_ID

    def detected(self) -> list[DeviceEntry]:
        detected = {device.port_name: device for device in detector.detect()}
        if not detected:
            return []
        entries = [
            DeviceEntry(
                self.id,
                "autodetect",
                None,
                "Autodetect" + (f" ({len(detected)} analyzers)" if len(detected) > 1 else ""),
            )
        ]
        for port, device in detected.items():
            serial = f", S/N {device.serial_number}" if device.serial_number else ""
            entries.append(serial_entry(port, f"PiPiLogicAnalyzer on {port}{serial}"))
        return entries

    def manual_entries(self) -> list[DeviceEntry]:
        return [
            DeviceEntry(self.id, "network", None, "Network device..."),
            DeviceEntry(self.id, "multi", None, "Multiple devices..."),
        ]

    def connect(self, entry: DeviceEntry, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        if entry.kind == "serial":
            return PiPiLogicAnalyzerDriver(entry.value)
        if entry.kind == "network":
            return self._connect_network(parent)
        if entry.kind == "multi":
            return self._connect_multi(parent)
        return self._connect_autodetect(parent)

    def idle_notice(self) -> Optional[str]:
        """Boards in bootloader mode or with foreign firmware."""
        drives = firmware_images.find_boot_drives()
        if drives:
            drive = drives[0]
            return (
                f"<b>{drive.chip} board in bootloader mode</b> ({drive.name}): "
                "install the PiPiLogicAnalyzer firmware to use it."
            )
        foreign = detector.detect_foreign_picos()
        if foreign:
            board = foreign[0]
            return (
                f"<b>Raspberry Pi board with {html.escape(board.description)}</b> on "
                f"{html.escape(board.port_name)}: it does not run the PiPiLogicAnalyzer firmware."
            )
        return None

    # ------------------------------------------------------------ connecting
    def _connect_network(self, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        dialog = NetworkConnectDialog(parent=parent)
        if not dialog.exec():
            return None
        return PiPiLogicAnalyzerDriver(f"{dialog.address}:{dialog.port}")

    def _connect_multi(self, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        dialog = MultiConnectDialog([port for port in detector.list_port_infos() if port.is_analyzer], parent)
        if not dialog.exec():
            return None
        return MultiAnalyzerDriver(dialog.connection_strings)

    def _connect_autodetect(self, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        devices = detector.detect()
        if not devices:
            messages.warning(
                parent,
                "Connect",
                "No analyzer was found.",
                "Check the USB cable. A board without the PiPiLogicAnalyzer firmware can be "
                "flashed with Device > Install or update firmware.",
            )
            return None
        if len(devices) == 1:
            return PiPiLogicAnalyzerDriver(devices[0].port_name)

        known = _known_device_order(devices)
        if known is not None:
            return MultiAnalyzerDriver(known)

        choice = messages.choose(
            parent,
            "Several analyzers found",
            f"{len(devices)} analyzers are connected. How do you want to use them?",
            ["Combine into a multi device set", f"Use only {devices[0].port_name}"],
            "A multi device set captures on all boards at once; the first board is the master "
            "and triggers the others.",
        )
        if choice is None:
            return None
        if choice == 1:
            return PiPiLogicAnalyzerDriver(devices[0].port_name)

        dialog = MultiComposeDialog(devices, parent)
        if not dialog.exec():
            return None

        _store_known_device(dialog.ordered_devices)
        return MultiAnalyzerDriver([device.port_name for device in dialog.ordered_devices])
