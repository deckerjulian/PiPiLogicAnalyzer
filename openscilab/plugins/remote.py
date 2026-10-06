# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Built-in plugin: remote devices (``driver/remote``, ``docs/remote.md``).

``remote:<name>``
    A script on another computer that connected to the server of openSciLab.
``remote-sim:climate``
    A simulated remote device (``climate``, ``audio``, ``echo``, ``daq``; options after ``?``).
"""

from __future__ import annotations

from typing import Callable, Optional

from ..driver import kinds


def open_remote(rest: str):
    from ..driver.remote.instrument import open_remote as open_device

    return open_device(f"remote:{rest}")


def open_simulated(rest: str, clock: Optional[Callable[[], float]] = None, seed: int = 1):
    from ..driver.remote.simulated import open_simulated_remote

    return open_simulated_remote(f"remote-sim:{rest}", circuit_clock=clock, seed=seed)


kinds.register("remote", instrument=open_remote, title="Remote device", simulation=None)
kinds.register("remote-sim", instrument=open_simulated, title="Simulated remote device", simulator=True)


def setup_ui() -> None:
    from ..ui.devices import register_backend
    from ..ui.devices.remote import RemoteBackend, RemoteSimulatorBackend

    register_backend(RemoteBackend())
    register_backend(RemoteSimulatorBackend())
