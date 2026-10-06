# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Delivers the events of the (Qt-free) device hub to the user interface thread."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from ...core.hub import Hub, HubEvent


class HubBridge(QObject):
    """``changed`` is emitted in the thread of the bridge for every event of the hub."""

    changed = Signal(object)

    def __init__(self, hub: Hub, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.hub = hub
        self._unsubscribe = hub.subscribe(self._forward)
        self.destroyed.connect(lambda *_args, unsubscribe=self._unsubscribe: unsubscribe())

    def _forward(self, event: HubEvent) -> None:
        try:
            self.changed.emit(event)
        except RuntimeError:  # the bridge is being deleted
            pass

    def close(self) -> None:
        self._unsubscribe()
