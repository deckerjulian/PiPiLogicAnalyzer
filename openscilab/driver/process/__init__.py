# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Devices read in a process of their own (``docs/timing.md``, *Staying responsive*).

A hardware device runs in a *device process*: its driver and facets live there and read the device
with nothing of the application beside them; the application gets an instrument that passes every
call on (:mod:`.proxy`), the samples arrive through shared memory. Preference ``devices.process``
(on by default), for the kinds of devices registered with ``process=True`` (``driver/kinds``: devices
on USB or a link that drops what is not read in time); remote devices and the Rigol (network) stay
in the application. A simulator of the device list that emulates a USB link runs in one too, as the
device it simulates (preference ``devices.simulator_process``, :func:`simulator_wanted`); one that
knows the time of its samples and those of flows stay in the application.
"""

from __future__ import annotations

from .proxy import (
    Connection,
    ProcessDriver,
    ProcessEnded,
    ProcessInstrument,
    RemoteObject,
    open_instrument,
)

#: set in a device process: what it opens, it opens itself
INSIDE = False


def wanted(address: str) -> bool:
    """Whether the device at ``address`` is opened in a device process (preference and kind)."""
    from ...core import preferences

    if INSIDE or not preferences.get("devices.process"):
        return False
    from .. import kinds

    kind = kinds.find(kinds.split(address)[0])
    return kind is not None and kind.process


#: the function a device process opens a simulator with: as it was last time (``driver/simulated/stored``)
SIMULATOR_FACTORY = "openscilab.driver.simulated.stored:open_stored"


def simulator_wanted(address: str) -> bool:
    """Whether the simulator at ``address`` (``sim:pico``) is opened in a device process: when it
    emulates a USB link, as the device it simulates would be read (preference)."""
    from ...core import preferences

    if INSIDE or not address.startswith("sim:") or not preferences.get("devices.simulator_process"):
        return False
    from ..simulated.stored import emulates_usb

    return emulates_usb(address)


__all__ = [
    "Connection",
    "ProcessDriver",
    "ProcessEnded",
    "ProcessInstrument",
    "RemoteObject",
    "open_instrument",
    "simulator_wanted",
    "wanted",
]
