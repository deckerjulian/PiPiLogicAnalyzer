# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Remote devices in the device list: the devices connected to the server (``remote:<name>``), the
simulated ones (``remote-sim:<demo>``) and an entry that opens the settings of the server."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...core.instrument import Instrument
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "remote"
SIM_BACKEND_ID = "remote-sim"
DEMO_TITLES = {"climate": "Simulation: remote climate station", "audio": "Simulation: remote microphone",
               "echo": "Simulation: remote echo",
               # the timings a remote device can have: a DAQ behind USB on another computer (drifting
               # sample clock, a sync line, a loopback), its clock following UTC as with PTP, its
               # sample clock shared with openSciLab's instruments
               "daq": "Simulation: remote DAQ (drifting clock, USB, sync line)",
               "daq?timescale=utc": "Simulation: remote DAQ, clock follows UTC (PTP)",
               "daq?shared_clock=1": "Simulation: remote DAQ, shared sample clock"}
#: names of the devices (the titles say what they simulate)
DEMO_NAMES = {"daq": "Remote DAQ", "daq?timescale=utc": "Remote DAQ (UTC)",
              "daq?shared_clock=1": "Remote DAQ (shared clock)"}


class RemoteBackend(DeviceBackend):
    id = BACKEND_ID

    def detected(self) -> list[DeviceEntry]:
        from ...driver.remote import servers

        server = servers.running_main()
        if server is None:
            return []
        entries = []
        for connection in server.devices():
            state = connection.clock.state()
            accuracy = f", ± {state.uncertainty * 1000:.1f} ms" if state.uncertainty < 1 else ""
            entries.append(DeviceEntry(BACKEND_ID, "device", connection.name,
                                       f"{connection.name} (remote, {connection.address[0]}{accuracy})"))
        return entries

    def manual_entries(self) -> list[DeviceEntry]:
        from ...driver.remote import servers

        running = servers.running_main()
        label = (f"Remote devices: port {running.port}..." if running is not None
                 else "Remote devices (off): settings...")
        return [DeviceEntry(BACKEND_ID, "settings", None, label)]

    def open_instrument(self, entry: DeviceEntry, parent: QWidget) -> Optional[Instrument]:
        from ...driver.remote import servers
        from ...driver.remote.instrument import make_instrument

        if entry.kind == "settings":
            window = parent.window() if parent is not None else None
            show = getattr(window, "show_preferences", None)
            if show is not None:
                show("Remote devices")
            return None
        found = servers.find(str(entry.value))
        if found is None:
            raise ValueError(f"{entry.value} is no longer connected")
        return make_instrument(found[0], str(entry.value), uri=f"remote:{entry.value}")

    def connect(self, entry: DeviceEntry, parent: QWidget):
        raise ValueError("a remote device has no capture driver")


class RemoteSimulatorBackend(DeviceBackend):
    id = SIM_BACKEND_ID

    def manual_entries(self) -> list[DeviceEntry]:
        return [DeviceEntry(SIM_BACKEND_ID, "demo", name, title, simulated=True)
                for name, title in DEMO_TITLES.items()]

    def open_instrument(self, entry: DeviceEntry, parent: QWidget) -> Optional[Instrument]:
        from ...driver.remote.simulated import open_simulated_remote
        from .. import background

        hub = getattr(parent, "hub", None)
        taken = {instrument.uri for instrument in hub.instruments()} if hub is not None else set()
        from ...driver.remote.simulated import address as remote_address
        from ...driver.remote.simulated import parse

        demo_name, _instance, options = parse(f"remote-sim:{entry.value}")
        address, number = remote_address(demo_name, 1, options), 1
        while address in taken:
            number += 1
            address = remote_address(demo_name, number, options)
        instrument = background.run(parent, "Starting the simulated device...", lambda: open_simulated_remote(address))
        title = DEMO_NAMES.get(str(entry.value)) or DEMO_TITLES.get(str(entry.value), address).replace("Simulation: ", "")
        instrument.name = title[:1].upper() + title[1:] + (f" ({number})" if number > 1 else "")
        return instrument

    def connect(self, entry: DeviceEntry, parent: QWidget):
        raise ValueError("a remote device has no capture driver")
