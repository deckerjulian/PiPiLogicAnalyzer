# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Built-in plugin: the simulators (``driver/simulated``) and the emulated analyzer.

``sim:free``
    A simulated instrument of a simulator profile (``sim:<profile>``, ``sim:pico*2``, ``sim:uno#2``).
``emulated``
    No hardware: the test signals of the simulation (trigger type ``SIMULATION``).
"""

from __future__ import annotations

from typing import Callable, Optional

from ..driver import kinds
from ..driver.base import AnalyzerDriverBase, DeviceConnectionError


def open_driver(rest: str) -> AnalyzerDriverBase:
    from ..driver.simulated import open_simulated

    try:
        return open_simulated(rest or "free").simulated_driver
    except ValueError as error:
        raise DeviceConnectionError(str(error)) from None


def open_instrument(rest: str, clock: Optional[Callable[[], float]] = None, fast: bool = False, seed: int = 1,
                    wiring: Optional[list] = None, signals: Optional[dict] = None):
    from ..driver.simulated import open_simulated

    return open_simulated(rest or "free", clock=clock, fast=fast, seed=seed, wiring=wiring, signals=signals)


def open_emulated(rest: str) -> AnalyzerDriverBase:
    from ..driver.emulated import EmulatedAnalyzerDriver

    if rest:
        raise DeviceConnectionError(f"The emulated analyzer has no address: emulated, not emulated:{rest}.")
    return EmulatedAnalyzerDriver()


kinds.register("sim", open_driver, title="Simulator", instrument=open_instrument, simulator=True)
kinds.register("emulated", open_emulated, title="Emulated analyzer")


def setup_ui() -> None:
    from ..ui.devices import register_backend
    from ..ui.devices.simulated import SimulatedBackend

    register_backend(SimulatedBackend())
