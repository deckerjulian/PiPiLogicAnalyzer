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
on USB or a link that drops what is not read in time); simulators, remote devices and the Rigol
(network) stay in the application.
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


__all__ = [
    "Connection",
    "ProcessDriver",
    "ProcessEnded",
    "ProcessInstrument",
    "RemoteObject",
    "open_instrument",
    "wanted",
]
