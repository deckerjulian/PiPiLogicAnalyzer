# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Kinds of devices (``docs/drivers.md``, *Plugins*).

A kind is the part of an address before the colon (``mydevice:/dev/cu.usbmodem1``). A plugin
registers it with the function that opens a device of that kind - the plugins that come with
openSciLab (:mod:`openscilab.plugins`: the Pico boards, the DSLogic, the simulators, ...) as well as
those of the user. From then on the device list, the flows (``address: mydevice:...``),
``openscilab-cli`` and the Python API open it - in a device process when ``process`` is set, by a
simulator profile with ``--sim``::

    from openscilab.driver import kinds

    kinds.register("mydevice", open_mydevice, title="My analyzer", detect=find_mydevices, process=True)

An address without a kind (a serial port, ``host:port``, a list of them) is one of :data:`DEFAULT`.

Qt free.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .base import AnalyzerDriverBase

#: the kind of an address without one: a serial port, ``host:port`` or a comma separated list
DEFAULT = "pico"

_NAME = re.compile(r"^[a-z][a-z0-9_.-]+$")


@dataclass(frozen=True)
class DeviceKind:
    """A kind of device a plugin registered."""

    #: the part of the address before the colon, lower case (``"mydevice"``)
    kind: str
    #: opens the device: the rest of the address (after ``kind:``, may be empty) → its driver; raises
    #: ``DeviceConnectionError`` (or ``OSError``/``ValueError``) when it cannot. It gets the keyword
    #: options of ``discovery.open_device`` it takes (``download_bitstream``). ``None``: the kind has
    #: instruments only (``instrument``)
    open: Optional[Callable[..., AnalyzerDriverBase]]
    #: for people (``"My analyzer"``)
    title: str = ""
    #: the connected devices of this kind: ``[(rest of the address, label), ...]``; they are listed
    #: in the device list and by ``openscilab-cli devices``
    detect: Optional[Callable[[], list[tuple[str, str]]]] = None
    #: read in a device process of its own (devices on USB or a serial port that stream)
    process: bool = False
    #: the simulator profile standing in for it (``--sim``, *Simulate* of a flow); ``None``: none can
    simulation: Optional[str] = "free"
    #: opens the device as an instrument with all its facets, in place of a driver with a capture
    #: facet: the rest of the address → the instrument. It gets what a flow passes on and it takes:
    #: ``clock`` (the circuit's time in seconds, a function), ``fast``, ``seed``, ``wiring``, ``signals``
    #: (``lab.engine.devices.open_instrument``)
    instrument: Optional[Callable[..., Any]] = None
    #: a simulator itself: a simulated flow opens it as it is
    simulator: bool = False
    #: the module that registered it: a device process imports it before opening
    module: str = ""

    @property
    def builtin(self) -> bool:
        """Registered by a plugin that comes with openSciLab."""
        return self.module.startswith("openscilab.")


_kinds: dict[str, DeviceKind] = {}


def register(kind: str, open: Optional[Callable[..., AnalyzerDriverBase]] = None, *, title: str = "",
             detect: Optional[Callable[[], list[tuple[str, str]]]] = None, process: bool = False,
             simulation: Optional[str] = "free", instrument: Optional[Callable[..., Any]] = None,
             simulator: bool = False) -> DeviceKind:
    """Add a kind of device (see the module). Registering a kind again replaces it; the kinds of
    openSciLab itself cannot be replaced."""
    from .. import plugins

    plugins.load_built_in()  # (their kinds come first)
    name = kind.strip().lower()
    if not _NAME.match(name):
        raise ValueError(f"'{kind}' cannot name a kind of device: use letters, digits, '-', '_' or '.', "
                         "starting with a letter (at least two characters)")
    if open is None and instrument is None:
        raise ValueError(f"the kind {name} needs a function that opens its devices")
    entry = DeviceKind(name, open, title or name, detect, bool(process), simulation, instrument, bool(simulator),
                       getattr(open or instrument, "__module__", "") or "")
    current = _kinds.get(name)
    if current is not None and current.builtin and not entry.builtin:
        raise ValueError(f"'{name}' is a kind of device of openSciLab itself")
    _kinds[name] = entry
    return entry


def unregister(kind: str) -> None:
    _kinds.pop(kind.strip().lower(), None)


def find(kind: str) -> Optional[DeviceKind]:
    """The registered kind ``kind``, or ``None``. The plugins of the user are loaded for a kind that
    is not one of openSciLab's own."""
    from .. import plugins

    name = kind.strip().lower()
    plugins.load_built_in()
    if name not in _kinds:
        plugins.load()
    return _kinds.get(name)


def registered() -> list[DeviceKind]:
    """Every registered kind (after loading the plugins), those of openSciLab first."""
    from .. import plugins

    plugins.load()
    return list(_kinds.values())


def split(address: str) -> tuple[str, str]:
    """The kind of ``address`` and the rest: ``("arduino", "COM5")`` for ``arduino:COM5``,
    ``("dslogic", "")`` for ``dslogic``, ``(DEFAULT, address)`` for an address without a kind
    (``COM5``, ``192.168.1.5:4045``)."""
    from .. import plugins

    address = address.strip()
    name, colon, rest = address.partition(":")
    name = name.strip().lower()
    if colon and _NAME.match(name) and (not rest.isdigit() or find(name) is not None):  # (counter:8: no port)
        return name, rest
    plugins.load_built_in()
    if not colon and name in _kinds:  # (a kind of openSciLab without a rest: ``dslogic``, ``emulated``)
        return name, ""
    return DEFAULT, address


def call(function: Callable[..., Any], rest: str, **options: Any) -> Any:
    """``function(rest, ...)`` with those of ``options`` it takes (an opener of a kind)."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):  # (no signature: everything)
        return function(rest, **options)
    if not any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters):
        names = {parameter.name for parameter in parameters}
        options = {name: value for name, value in options.items() if name in names}
    return function(rest, **options)
