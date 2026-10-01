# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The kinds of devices the main window can connect to.

A :class:`DeviceBackend` lists the devices of one kind in the device list and opens the one the
user picks, asking for what it needs (an address, a missing file, ...). The main window only
talks to the backends returned by :func:`backends` and to the :class:`AnalyzerDriverBase` a
backend opens. ``docs/drivers.md`` explains how to add a device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtWidgets import QWidget

from ...driver.base import AnalyzerDriverBase


@dataclass(frozen=True)
class DeviceEntry:
    """One entry of the device list; ``kind`` and ``value`` mean something to its backend only."""

    backend: str
    kind: str
    value: Optional[str] = None
    label: str = field(default="", compare=False)


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

    def connect(self, entry: DeviceEntry, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        """Opens the device of ``entry``; ``None`` when the user cancelled.

        Raises ``DeviceConnectionError``, ``OSError`` or ``ValueError`` when the device cannot be
        opened; the main window reports them.
        """
        raise NotImplementedError

    def idle_notice(self) -> Optional[str]:
        """A device of this kind that cannot be used yet (e.g. without firmware), while none is connected."""
        return None


_added: list[DeviceBackend] = []


def register_backend(backend: DeviceBackend) -> None:
    """Adds the backend of a device that is not built in; windows created afterwards list it."""
    _added.append(backend)


def backends() -> list[DeviceBackend]:
    """Every backend, in the order of the device list."""
    from .dslogic import DSLogicBackend
    from .pico import PicoBackend

    return [PicoBackend(), DSLogicBackend(), *_added]
