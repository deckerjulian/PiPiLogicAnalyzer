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

from ...core import settings
from ...core.hub import Hub
from ...core.instrument import Instrument, InstrumentStatus
from ...driver.simulated import free_address, open_simulated, scenarios
from ...driver.simulated.profiles import available_profiles, profile_title
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


SIGNALS_FILE = "sim-signals.json"
#: wires and the emulated USB link of each simulator (by its address)
CIRCUIT_FILE = "sim-circuit.json"
#: the entry of the device list that asks for a multi device set of simulated boards
KIND_MULTI = "multi"


def stored_signals(uri: str) -> Optional[dict]:
    """What the simulator at ``uri`` was last told to simulate."""
    data = settings.get_settings(SIGNALS_FILE)
    config = data.get(uri) if isinstance(data, dict) else None
    return dict(config) if isinstance(config, dict) else None


def remember_signals(uri: str, config: dict) -> None:
    data = settings.get_settings(SIGNALS_FILE)
    data = data if isinstance(data, dict) else {}
    data[uri] = config
    settings.persist_settings(SIGNALS_FILE, data)


def stored_circuit(uri: str) -> dict:
    """The wires (``wiring``) and the USB link (``usb``: values, or ``False`` for none) the simulator
    at ``uri`` was given; empty when nothing was changed."""
    data = settings.get_settings(CIRCUIT_FILE)
    entry = data.get(uri) if isinstance(data, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def remember_circuit(uri: str, **values) -> None:
    """Keep ``wiring`` and/or ``usb`` for the simulator at ``uri`` (see :func:`stored_circuit`)."""
    data = settings.get_settings(CIRCUIT_FILE)
    data = data if isinstance(data, dict) else {}
    entry = dict(data.get(uri) or {})
    entry.update(values)
    data[uri] = entry
    settings.persist_settings(CIRCUIT_FILE, data)


def apply_circuit(instrument: Instrument) -> None:
    """Give a simulator the wires and the USB link it had last time."""
    from ...driver.simulated import set_usb, set_wiring

    driver = getattr(instrument, "simulated_driver", None)
    stored = stored_circuit(instrument.uri) if driver is not None else {}
    if "usb" in stored:
        try:
            set_usb(driver, stored["usb"] or None)
        except (TypeError, ValueError):
            pass
    if isinstance(stored.get("drift"), (int, float)):
        driver.set_drift(float(stored["drift"]))
    if stored.get("wiring"):
        try:
            set_wiring(driver, list(stored["wiring"]))
        except (TypeError, ValueError):
            pass  # a pin of another profile: the wires of the profile


def open_at(address: str, hub: Optional[Hub] = None) -> Instrument:
    """The simulator at ``address`` (``sim:uno``, ``sim:pico*2#2``), simulating what it did last
    time. In real time on the clock of the hub: simulators wired to each other agree on it."""
    spec = address[4:] if address.startswith("sim:") else address
    instrument = open_simulated(spec, clock=hub.now if hub is not None else None)
    config = stored_signals(instrument.uri)
    if config:
        try:
            scenarios.apply(instrument.simulated_driver, config)
        except scenarios.ScenarioError:
            pass  # e.g. a capture file that is gone: the test signals of the profile
    apply_circuit(instrument)
    return instrument


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
        return open_at(str(free_address(spec, taken)), hub)

    def connect(self, entry: DeviceEntry, parent: QWidget):
        instrument = self.open_instrument(entry, parent)
        return instrument.capture.driver if instrument is not None else None


def is_simulated(instrument: Instrument) -> bool:
    return getattr(instrument, "simulated_driver", None) is not None


def inject(hub: Hub, instrument: Instrument, fault: str, value: float = 0.0) -> None:
    """Inject ``fault`` into the simulated ``instrument``; its status in the hub follows."""
    instrument.simulated_driver.inject(fault, value)
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
