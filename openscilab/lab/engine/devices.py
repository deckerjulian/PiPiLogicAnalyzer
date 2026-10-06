# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Opening the devices of a flow by their address (``sim:uno``, ``pico:/dev/cu.usbmodem1``)."""

from __future__ import annotations

from typing import Optional

from ...core.instrument import Instrument


def simulation_profile_for(address: str) -> str:
    """The simulator profile standing in for a real device (``--sim``): the one of its kind
    (``driver/kinds``), ``free`` when there is none."""
    from ...driver import kinds
    from ...driver.simulated.profiles import available_profiles

    kind = kinds.find(kinds.split(address)[0])
    wanted = kind.simulation if kind is not None else None
    return wanted if wanted in available_profiles() else "free"


def open_instrument(address: str, simulate: bool = False, clock=None, fast: bool = False,
                    seed: int = 1, wiring: Optional[list] = None, signals: Optional[dict] = None) -> Instrument:
    """The instrument at ``address``; with ``simulate`` every device is a simulator (``wiring``:
    more wires of its circuit, e.g. from ``simulation.wiring`` of the project; ``signals``: what
    a simulator simulates, see :mod:`openscilab.driver.simulated.scenarios`)."""
    from ...driver import discovery, kinds, process

    name, rest = kinds.split(address)
    kind = kinds.find(name)
    now: Optional[object] = clock.now if clock is not None else None
    if simulate and not (kind is not None and kind.simulator):
        if kind is not None and kind.simulation is None:
            raise ValueError(f"{address}: a {kind.title} has no simulator; use a simulated device in its place "
                             "(the simulators of the device list)")
        from ...driver.simulated import open_simulated

        return open_simulated(simulation_profile_for(address), clock=now, fast=fast, seed=seed, wiring=wiring,
                              signals=signals)
    if kind is not None and kind.instrument is not None:
        return kinds.call(kind.instrument, rest, clock=now, fast=fast, seed=seed, wiring=wiring, signals=signals)
    if process.wanted(address):
        # a device on USB is read in a process of its own: nothing the application does delays it
        return process.open_instrument(address)
    driver = discovery.open_device(address)
    return Instrument.from_driver(driver, uri=address)
