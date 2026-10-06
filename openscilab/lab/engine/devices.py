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
    """The simulator profile standing in for a real device (``--sim``)."""
    from ...driver.simulated.profiles import available_profiles

    kind = address.split(":", 1)[0].lower()
    from ...driver import kinds

    added = kinds.find(kind) if ":" in address and kind not in kinds.BUILT_IN else None
    if added is not None:
        return added.simulation if added.simulation in available_profiles() else "free"
    wanted = {"pico": "pico", "pico-net": "pico", "pico-multi": "pico", "rigol": "dho924s", "dho": "dho924s",
              "arduino": "uno"}.get(kind, "free")
    return wanted if wanted in available_profiles() else "free"


def open_instrument(address: str, simulate: bool = False, clock=None, fast: bool = False,
                    seed: int = 1, wiring: Optional[list] = None, signals: Optional[dict] = None) -> Instrument:
    """The instrument at ``address``; with ``simulate`` every device is a simulator (``wiring``:
    more wires of its circuit, e.g. from ``simulation.wiring`` of the project; ``signals``: what
    a simulator simulates, see :mod:`openscilab.driver.simulated.scenarios`)."""
    if address.startswith("remote-sim:"):
        from ...driver.remote.simulated import open_simulated_remote

        return open_simulated_remote(address, circuit_clock=clock.now if clock is not None else None, seed=seed)
    if address.startswith("remote:"):
        if simulate:
            raise ValueError("a remote device has no simulator of its own: use remote-sim:<demo> "
                             "(climate, audio, echo) for a simulated one")
        from ...driver.remote.instrument import open_remote

        return open_remote(address)
    if address.startswith("sim:") or simulate:
        from ...driver.simulated import open_simulated

        profile = address[4:] if address.startswith("sim:") else simulation_profile_for(address)
        now: Optional[object] = clock.now if clock is not None else None
        return open_simulated(profile, clock=now, fast=fast, seed=seed, wiring=wiring, signals=signals)
    from ...driver import discovery, process

    if process.wanted(address):
        # a device on USB is read in a process of its own: nothing the application does delays it
        return process.open_instrument(address)
    driver = discovery.open_device(address)
    return Instrument.from_driver(driver, uri=address)
