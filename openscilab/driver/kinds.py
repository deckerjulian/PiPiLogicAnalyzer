# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Kinds of devices that are not built in (``docs/drivers.md``, *Plugins*).

A kind is the part of an address before the colon (``mydevice:/dev/cu.usbmodem1``). A plugin
registers it with the function that opens a device of that kind; from then on the device list, the
flows (``address: mydevice:...``), ``openscilab-cli`` and the Python API open it like a built-in
device - in a device process when ``process`` is set, by a simulator profile with ``--sim``::

    from openscilab.driver import kinds

    kinds.register("mydevice", open_mydevice, title="My analyzer", detect=find_mydevices, process=True)

Qt free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .base import AnalyzerDriverBase

#: the kinds openSciLab opens itself (``driver/discovery``, ``lab/engine/devices``)
BUILT_IN = frozenset({
    "pico", "pico-net", "pico-multi", "dslogic", "emulated", "sim", "arduino", "arduino-sim", "rigol",
    "rigol-sim", "dho", "remote", "remote-sim",
})

_NAME = re.compile(r"^[a-z][a-z0-9_.-]+$")


@dataclass(frozen=True)
class DeviceKind:
    """A kind of device a plugin added."""

    #: the part of the address before the colon, lower case (``"mydevice"``)
    kind: str
    #: opens the device: the rest of the address (after ``kind:``) → its driver; raises
    #: ``DeviceConnectionError`` (or ``OSError``/``ValueError``) when it cannot
    open: Callable[[str], AnalyzerDriverBase]
    #: for people (``"My analyzer"``)
    title: str = ""
    #: the connected devices of this kind: ``[(rest of the address, label), ...]``; they are listed
    #: in the device list and by ``openscilab-cli devices``
    detect: Optional[Callable[[], list[tuple[str, str]]]] = None
    #: read in a device process of its own (devices on USB or a serial port that stream)
    process: bool = False
    #: the simulator profile standing in for it (``--sim``, *Simulate* of a flow)
    simulation: str = "free"
    #: the module that registered it: a device process imports it before opening
    module: str = ""


_kinds: dict[str, DeviceKind] = {}


def register(kind: str, open: Callable[[str], AnalyzerDriverBase], *, title: str = "",
             detect: Optional[Callable[[], list[tuple[str, str]]]] = None, process: bool = False,
             simulation: str = "free") -> DeviceKind:
    """Add a kind of device (see the module). Registering a kind again replaces it."""
    name = kind.strip().lower()
    if not _NAME.match(name):
        raise ValueError(f"'{kind}' cannot name a kind of device: use letters, digits, '-', '_' or '.', "
                         "starting with a letter (at least two characters)")
    if name in BUILT_IN:
        raise ValueError(f"'{name}' is a kind of device of openSciLab itself")
    entry = DeviceKind(name, open, title or name, detect, bool(process), simulation or "free",
                       getattr(open, "__module__", "") or "")
    _kinds[name] = entry
    return entry


def unregister(kind: str) -> None:
    _kinds.pop(kind.strip().lower(), None)


def find(kind: str) -> Optional[DeviceKind]:
    """The registered kind ``kind`` (after loading the plugins), or ``None``."""
    from .. import plugins

    plugins.load()
    return _kinds.get(kind.strip().lower())


def registered() -> list[DeviceKind]:
    """Every registered kind (after loading the plugins), by name."""
    from .. import plugins

    plugins.load()
    return [_kinds[name] for name in sorted(_kinds)]


def kind_of(address: str) -> str:
    return address.split(":", 1)[0].strip().lower() if ":" in address else ""


def looks_like_kind(address: str) -> bool:
    """Whether ``address`` starts with a kind (``name:...``) rather than being a port or a host and
    port (``COM5``, ``/dev/ttyACM0``, ``C:\\...``, ``192.168.1.5:4045``)."""
    kind, _, rest = address.partition(":")
    return bool(rest) and bool(_NAME.match(kind.lower())) and not rest.isdigit()
