# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Built-in plugin: Rigol DHO900 oscilloscopes over the network (``driver/rigoldho``).

``rigol:192.168.1.20``
    An oscilloscope (through its bridge app when it runs; ``rigol:<address>:<port>`` for another port).
``rigol-sim:bridge``
    A simulated DHO924S with the bridge app (``rigol-sim:direct`` without it).
"""

from __future__ import annotations

from ..driver import kinds
from ..driver.base import AnalyzerDriverBase, DeviceConnectionError


def open_rigol(rest: str) -> AnalyzerDriverBase:
    from ..driver.rigoldho.driver import open_network

    if not rest.strip():
        raise DeviceConnectionError("Give the address of the oscilloscope: rigol:<address>.")
    return open_network(rest)


def open_simulated(rest: str) -> AnalyzerDriverBase:
    from ..driver.simulated.scpi_shell import open_shell

    try:
        return open_shell(rest or "bridge")
    except ValueError as error:
        raise DeviceConnectionError(str(error)) from None


kinds.register("rigol", open_rigol, title="Rigol DHO900", simulation="dho924s")
kinds.register("rigol-sim", open_simulated, title="Simulated Rigol DHO924S", simulation="dho924s")


def setup_ui() -> None:
    from ..ui.devices import register_backend
    from ..ui.devices.rigoldho import RigolBackend

    register_backend(RigolBackend())
