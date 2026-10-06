# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The kinds of devices the lab can connect to.

A :class:`DeviceBackend` lists the devices of one kind and opens the one the user picks, asking
for what it needs (an address, a missing file, ...). It returns an
:class:`~openscilab.core.instrument.Instrument` (:meth:`DeviceBackend.open_instrument`), which
the device hub keeps; for the logic analyzer drivers that is an instrument with a capture facet
around the :class:`AnalyzerDriverBase` of :meth:`DeviceBackend.connect`. ``docs/drivers.md``
explains how to add a device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtWidgets import QWidget

from ...core.instrument import Instrument
from ...driver.base import AnalyzerDriverBase


@dataclass(frozen=True)
class DeviceEntry:
    """One entry of the device list; ``kind`` and ``value`` mean something to its backend only."""

    backend: str
    kind: str
    value: Optional[str] = None
    label: str = field(default="", compare=False)
    #: a simulated device: listed with the simulators (their group of the device list)
    simulated: bool = field(default=False, compare=False)


class DeviceBackend:
    """Lists and opens the devices of one kind."""

    #: Unique name, stored in the entries of this backend
    id: str = ""

    def detected(self) -> list[DeviceEntry]:
        """The connected devices, listed at the top of the device list."""
        return []

    def manual_entries(self) -> list[DeviceEntry]:
        """Entries that ask for the device when chosen (e.g. a network address), listed last."""
        return []

    def address(self, entry: DeviceEntry, parent: QWidget) -> Optional[str]:
        """The address of the device of ``entry`` for ``discovery.open_device`` (``pico:COM5``), after
        asking what is needed; ``None`` when the user cancelled. Backends that have one are opened by
        it - devices on USB in a device process (``driver/process``)."""
        raise NotImplementedError

    def connect(self, entry: DeviceEntry, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        """Opens the device of ``entry``; ``None`` when the user cancelled.

        Raises ``DeviceConnectionError``, ``OSError`` or ``ValueError`` when the device cannot be
        opened; the main window reports them.
        """
        raise NotImplementedError

    def open_instrument(self, entry: DeviceEntry, parent: QWidget) -> Optional[Instrument]:
        """Opens the device of ``entry`` as an instrument; ``None`` when the user cancelled.

        A backend with :meth:`address` opens the device by its address: in a device process when it
        is a device on USB (preference ``devices.process``), else here. Otherwise the driver of
        :meth:`connect` with a capture facet; backends of instruments with more facets override it.
        Raises what :meth:`connect` raises.
        """
        from .. import background

        if type(self).address is not DeviceBackend.address:
            address = self.address(entry, parent)
            if address is None:
                return None
            from ...driver import process

            if process.wanted(address):
                return background.run(parent, f"Connecting to {address}...", lambda: process.open_instrument(address))
            driver = open_address(parent, address)
        else:
            driver = self.connect(entry, parent)
        if driver is None:
            return None
        # the address the driver found (autodetect, network, multi), not the way it was found;
        # making the instrument asks the device for its capabilities: not in the window's thread
        uri = getattr(driver, "address", None) or entry_uri(entry)
        try:
            return background.run(parent, "Reading the device...", lambda: Instrument.from_driver(driver, uri=uri))
        except background.Cancelled:
            driver.dispose()
            raise

    def idle_notice(self) -> Optional[str]:
        """A device of this kind that cannot be used yet (e.g. without firmware), while none is connected."""
        return None


def open_address(parent: QWidget, address: str) -> AnalyzerDriverBase:
    """The driver of the device at ``address``, opened without freezing the window."""
    from ...driver import discovery
    from .. import background

    return background.run(parent, f"Connecting to {address}...", lambda: discovery.open_device(address))


def entry_uri(entry: DeviceEntry) -> str:
    """Address of an entry as in the CLI (``pico:/dev/cu.usbmodem1``)."""
    return f"{entry.backend}:{entry.value}" if entry.value else f"{entry.backend}:{entry.kind}"


_added: list[DeviceBackend] = []


def register_backend(backend: DeviceBackend) -> None:
    """Adds the backend of a device that is not built in (a plugin's ``setup_ui()``, see
    :mod:`openscilab.plugins`); windows created afterwards list it."""
    _added.append(backend)


class KindBackend(DeviceBackend):
    """The devices of a kind a plugin registered (``driver/kinds``): what its ``detect`` finds, opened
    by their address. A plugin with a backend of its own for the kind replaces it."""

    def __init__(self, kind) -> None:
        self.kind = kind
        self.id = kind.kind

    def detected(self) -> list[DeviceEntry]:
        from ...driver import discovery

        return [DeviceEntry(self.id, "device", info.id.split(":", 1)[1], info.label)
                for info in discovery.detect_kind(self.kind)]

    def address(self, entry: DeviceEntry, parent: QWidget) -> Optional[str]:
        return f"{self.id}:{entry.value}"


def backends() -> list[DeviceBackend]:
    """Every backend, in the order of the device list."""
    from ...driver import kinds
    from .arduino import ArduinoBackend
    from .dslogic import DSLogicBackend
    from .pico import PicoBackend
    from .remote import RemoteBackend, RemoteSimulatorBackend
    from .rigoldho import RigolBackend
    from .simulated import SimulatedBackend

    own = {backend.id for backend in _added}
    return [PicoBackend(), ArduinoBackend(), DSLogicBackend(), RigolBackend(), RemoteBackend(), SimulatedBackend(),
            RemoteSimulatorBackend(), *_added,
            *[KindBackend(kind) for kind in kinds.registered() if kind.kind not in own]]
