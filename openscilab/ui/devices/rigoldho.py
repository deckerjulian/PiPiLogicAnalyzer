# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Rigol DHO900 oscilloscopes on the network: found by the beacon of their bridge app or entered
by address; and the simulated DHO924S with and without the bridge app."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...driver.base import AnalyzerDriverBase
from ...driver.rigoldho import beacon, scpi
from .. import background
from ..dialogs.device_dialogs import NetworkConnectDialog
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "rigol"
KIND_NETWORK = "network"
KIND_ADDRESS = "address"
KIND_SIMULATED = "sim"


class RigolBackend(DeviceBackend):
    id = BACKEND_ID

    def detected(self) -> list[DeviceEntry]:
        return [DeviceEntry(BACKEND_ID, KIND_NETWORK, f"{item.host}:{item.port}", item.label) for item in beacon.seen()]

    def manual_entries(self) -> list[DeviceEntry]:
        return [DeviceEntry(BACKEND_ID, KIND_ADDRESS, None, "Rigol oscilloscope by address..."),
                DeviceEntry(BACKEND_ID, KIND_SIMULATED, "bridge", "Simulation: DHO924S with bridge app", simulated=True),
                DeviceEntry(BACKEND_ID, KIND_SIMULATED, "direct", "Simulation: DHO924S without bridge app",
                            simulated=True)]

    def connect(self, entry: DeviceEntry, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        if entry.kind == KIND_SIMULATED:
            from ...driver.simulated.scpi_shell import open_shell

            return background.run(parent, "Starting the simulated oscilloscope...", lambda: open_shell(entry.value))
        from ...driver.rigoldho.driver import open_network

        address = entry.value
        if entry.kind == KIND_ADDRESS:
            dialog = NetworkConnectDialog(port=scpi.BRIDGE_PORT, parent=parent)
            dialog.setWindowTitle("Connect to a Rigol oscilloscope")
            if not dialog.exec():
                return None
            address = f"{dialog.address}:{dialog.port}"
        return background.run(parent, f"Connecting to {address}...", lambda: open_network(address))
