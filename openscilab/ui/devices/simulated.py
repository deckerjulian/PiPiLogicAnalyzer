# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Simulated instruments in the device list (*Simulation: Arduino Uno*, ...) and the fault
injection of *Devices → Simulate faults*."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QMenu, QWidget

from ...core.hub import Hub
from ...core.instrument import Instrument, InstrumentStatus
from ...driver.simulated import free_address
from ...driver.simulated.profiles import available_profiles, profile_title
from ...driver.simulated.stored import open_stored
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "sim"
#: (title, fault, value): what the simulation menu offers per simulated instrument
FAULTS = (
    ("Disconnect", "disconnect", 0.0),
    ("Reconnect", "reconnect", 0.0),
    ("Delay answers by 100 ms", "delay", 0.1),
    ("Answer without delay", "delay", 0.0),
    ("Overflow the next stream", "overflow", 0.0),
    ("Restart (outputs released)", "restart", 0.0),
)


#: the entry of the device list that asks for a multi device set of simulated boards
KIND_MULTI = "multi"


def open_at(address: str, hub: Optional[Hub] = None) -> Instrument:
    """The simulator at ``address`` (``sim:uno``, ``sim:pico*2#2``) in the application, simulating
    what it did last time. In real time on the clock of the hub: simulators wired to each other
    agree on it."""
    return open_stored(address, clock=hub.now if hub is not None else None)


def connect_simulator(address: str, hub: Optional[Hub], parent: Optional[QWidget]) -> Instrument:
    """The simulator at ``address`` as the device list connects it: in a device process when it
    emulates a USB link (``process.simulator_wanted``), else in the application (:func:`open_at`)."""
    from ...driver import process

    if process.simulator_wanted(address):
        from .. import background

        return background.run(parent, f"Starting the simulator {address}...",
                              lambda: process.open_instrument(address, factory=process.SIMULATOR_FACTORY))
    return open_at(address, hub)


class SimulatedBackend(DeviceBackend):
    """Every simulator profile as an entry of the device list. Each can be connected several
    times (the second Uno is ``sim:uno#2``); *Simulated multi device…* combines several boards."""

    id = BACKEND_ID

    def manual_entries(self) -> list[DeviceEntry]:
        entries = [DeviceEntry(BACKEND_ID, "profile", name, profile_title(name), simulated=True)
                   for name in available_profiles()]
        entries.append(DeviceEntry(BACKEND_ID, KIND_MULTI, None, "Simulated multi device...", simulated=True))
        return entries

    def open_instrument(self, entry: DeviceEntry, parent: QWidget) -> Optional[Instrument]:
        hub = getattr(parent, "hub", None)
        spec = entry.value
        if entry.kind == KIND_MULTI:
            from ..dialogs.simulated_multi_dialog import SimulatedMultiDialog

            dialog = SimulatedMultiDialog(parent)
            if not dialog.exec():
                return None
            spec = dialog.spec
        taken = [instrument.uri for instrument in hub.instruments()] if hub is not None else []
        return connect_simulator(str(free_address(spec, taken)), hub, parent)

    def connect(self, entry: DeviceEntry, parent: QWidget):
        instrument = self.open_instrument(entry, parent)
        return instrument.capture.driver if instrument is not None else None


def is_simulated(instrument: Instrument) -> bool:
    return instrument.simulation is not None


def inject(hub: Hub, instrument: Instrument, fault: str, value: float = 0.0) -> None:
    """Inject ``fault`` into the simulated ``instrument``; its status in the hub follows."""
    instrument.simulation.inject(fault, value)
    name = hub.name_of(instrument) if instrument in hub else None
    if name is None:
        return
    if fault == "disconnect":
        hub.set_status(name, InstrumentStatus.DISCONNECTED, "simulated fault: disconnected")
    elif fault in ("reconnect", "restart"):
        hub.set_status(name, InstrumentStatus.SIMULATED, "")


def fill_simulation_menu(menu: QMenu, hub: Hub) -> None:
    """The faults for every simulated instrument of the hub (rebuilt when the menu opens)."""
    menu.clear()
    simulated = [instrument for instrument in hub.instruments() if is_simulated(instrument)]
    if not simulated:
        menu.addAction("No simulated instrument is open").setEnabled(False)
        return
    for instrument in simulated:
        submenu = menu.addMenu(instrument.name)
        for title, fault, value in FAULTS:
            submenu.addAction(title, lambda fault=fault, value=value, instrument=instrument:
                              inject(hub, instrument, fault, value))
