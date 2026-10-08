# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""What each simulator of the device list was last told: its signals (``sim-signals.json``) and its
wires, USB link and clock drift (``sim-circuit.json``), by its address. :func:`open_stored` opens a
simulator with them - in the application or in a device process (``driver/process``). Qt free.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from ...core import settings
from ...core.instrument import Instrument
from . import scenarios

log = logging.getLogger(__name__)

SIGNALS_FILE = "sim-signals.json"
#: wires, the emulated USB link and the clock drift of each simulator (by its address)
CIRCUIT_FILE = "sim-circuit.json"


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
    """The wires (``wiring``), the USB link (``usb``: values, or ``False`` for none) and the clock
    drift (``drift``, ppm) the simulator at ``uri`` was given; empty when nothing was changed."""
    data = settings.get_settings(CIRCUIT_FILE)
    entry = data.get(uri) if isinstance(data, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def remember_circuit(uri: str, **values) -> None:
    """Keep ``wiring``, ``usb`` and/or ``drift`` for the simulator at ``uri`` (see :func:`stored_circuit`)."""
    data = settings.get_settings(CIRCUIT_FILE)
    data = data if isinstance(data, dict) else {}
    entry = dict(data.get(uri) or {})
    entry.update(values)
    data[uri] = entry
    settings.persist_settings(CIRCUIT_FILE, data)


def apply_stored(instrument: Instrument) -> None:
    """Give a simulator the signals, wires, USB link and drift it had last time."""
    simulation = instrument.simulation
    if simulation is None:
        return
    config = stored_signals(instrument.uri)
    if config:
        try:
            simulation.apply_signals(config)
        except scenarios.ScenarioError:
            pass  # e.g. a capture file that is gone: the test signals of the profile
    stored = stored_circuit(instrument.uri)
    if "usb" in stored:
        try:
            simulation.set_usb(stored["usb"] or None)
        except (TypeError, ValueError):
            pass
    if isinstance(stored.get("drift"), (int, float)):
        simulation.set_drift(float(stored["drift"]))
    if stored.get("wiring"):
        try:
            simulation.set_wiring(list(stored["wiring"]))
        except (TypeError, ValueError):
            pass  # a pin of another profile: the wires of the profile


def open_stored(address: str, clock: Optional[Callable[[], float]] = None) -> Instrument:
    """The simulator at ``address`` (``sim:uno``, ``sim:pico*2#2``), simulating what it did last time."""
    from . import open_simulated

    spec = address.removeprefix("sim:")
    instrument = open_simulated(spec, clock=clock)
    apply_stored(instrument)
    return instrument


def open_in_device_process(address: str) -> Instrument:
    """:func:`open_stored` for a device process (``driver/process``): on the system's monotonic
    clock, as the hub and the simulators of the application - all of them agree on the time."""
    import time

    return open_stored(address, clock=time.monotonic)


def emulates_usb(address: str) -> bool:
    """Whether the simulator at ``address`` emulates a USB link (its profile's, unless it was given
    another one); ``False`` for an address that is no simulator."""
    from .profiles import SimAddress, load_profile

    spec = address.removeprefix("sim:")
    try:
        profile = load_profile(SimAddress.parse(spec).profile)
    except (ValueError, KeyError, OSError):
        return False
    stored = stored_circuit(address)
    return bool(stored["usb"]) if "usb" in stored else bool(profile.get("usb"))
