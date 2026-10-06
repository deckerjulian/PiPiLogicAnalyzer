# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Undo and redo of what is changed in a data view: samples (delete, insert, paste, shift),
channels (names, colours, hidden, order, derived ones), regions, markers and buses.

A :class:`DataState` holds the state of the capture after an edit. The sample edits replace the
arrays of the channels (``np.concatenate``) instead of changing them, so a state only keeps
references to the arrays of that moment – no copies. The undo stack keeps fewer steps for large
captures (:func:`undo_limit`), because every step can hold the arrays it replaced.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from PySide6.QtGui import QUndoCommand

if TYPE_CHECKING:  # pragma: no cover
    from .dataview import DataView

#: steps kept for captures that are not large
UNDO_STEPS = 50
#: bytes of samples the undo steps of a capture may hold on to. A step that changed every
#: channel (deleting or inserting samples) keeps all the arrays it replaced, so the steps kept
#: follow the size of the capture: 4 for 20 million samples on 24 channels, 1 for the largest.
UNDO_BYTES = 2 << 30


def undo_limit(sample_count: int, channels: int = 1) -> int:
    """Undo steps kept for a capture of ``sample_count`` samples on ``channels`` channels."""
    size = max(int(sample_count), 1) * max(int(channels), 1)
    return max(1, min(UNDO_STEPS, UNDO_BYTES // size))


@dataclass
class ChannelState:
    channel: Any
    samples: Optional[np.ndarray]
    name: str
    color: Optional[int]
    hidden: bool


@dataclass
class DataState:
    """What an edit can change, as it was at one moment."""

    channels: list[ChannelState] = field(default_factory=list)
    pre_trigger: int = 0
    post_trigger: int = 0
    loop_count: int = 0
    measure_bursts: bool = False
    bursts: Any = None
    regions: list = field(default_factory=list)
    bookmarks: list[tuple[int, str]] = field(default_factory=list)
    buses: list = field(default_factory=list)
    pinned: frozenset = frozenset()


def take(view: "DataView") -> Optional[DataState]:
    """The state of ``view`` now (``None`` without data)."""
    model = view.model
    session = model.session
    if session is None:
        return None
    return DataState(
        channels=[ChannelState(channel, channel.samples, channel.channel_name, channel.channel_color, channel.hidden)
                  for channel in session.capture_channels],
        pre_trigger=session.pre_trigger_samples,
        post_trigger=session.post_trigger_samples,
        loop_count=session.loop_count,
        measure_bursts=session.measure_bursts,
        bursts=session.bursts,
        regions=[copy.copy(region) for region in model.regions],
        bookmarks=[(bookmark.sample, bookmark.name) for bookmark in model.bookmarks],
        buses=[copy.copy(bus) for bus in model.buses],
        pinned=frozenset(model._pinned),
    )


def restore(view: "DataView", state: DataState) -> None:
    """Put ``state`` back into ``view`` (its signals are no new edits)."""
    from ..view_model import Bookmark

    model = view.model
    session = model.session
    if session is None or state is None:
        return
    view._loading += 1
    try:
        session.capture_channels = [item.channel for item in state.channels]
        for item in state.channels:
            item.channel.samples = item.samples
            item.channel.channel_name = item.name
            item.channel.channel_color = item.color
            item.channel.hidden = item.hidden
        session.pre_trigger_samples = state.pre_trigger
        session.post_trigger_samples = state.post_trigger
        session.loop_count = state.loop_count
        session.measure_bursts = state.measure_bursts
        session.bursts = state.bursts
        model.set_regions([copy.copy(region) for region in state.regions])
        model._bookmarks = [Bookmark(sample, name) for sample, name in state.bookmarks]
        model.bookmarks_changed.emit()
        model.set_buses([copy.copy(bus) for bus in state.buses])
        model._pinned = set(state.pinned)
        model.rebuild_transitions()
        model.notify_channels_changed()
        model.notify_capture_changed()
    finally:
        view._loading -= 1
    view._state = state  # (the decoders run again through capture_changed)


class DataEdit(QUndoCommand):
    """One edit of a data view: the states before and after it."""

    def __init__(self, view: "DataView", text: str, before: DataState, after: DataState) -> None:
        super().__init__(text)
        self.view, self.before, self.after = view, before, after
        self._done = True  # the edit is already in the view when it is pushed

    def undo(self) -> None:
        restore(self.view, self.before)

    def redo(self) -> None:
        if self._done:
            self._done = False
            return
        restore(self.view, self.after)
