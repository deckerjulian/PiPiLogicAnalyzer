# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared state of the capture views.

The original code kept the viewer, the ruler, the previewer and the annotation
viewer in three parallel lists (``sampleDisplays``, ``regionDisplays``,
``markerDisplays``) and pushed every change to each of them by hand.  Here a
single observable model owns the state and the widgets subscribe to its signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import numpy as np
from PySide6.QtCore import QObject, Signal

from ..core.analysis import ChannelTransitions, build_transitions
from ..core.regions import SampleRegion
from ..core.sample_store import is_on_disk
from ..driver.models import AnalyzerChannel, BusDefinition, CaptureSession

MIN_VISIBLE_SAMPLES = 4
#: Height of a channel row in pixels: the rows grow to fill the view, this is their minimum.
DEFAULT_CHANNEL_HEIGHT = 48
SMALLEST_CHANNEL_HEIGHT = 18
LARGEST_CHANNEL_HEIGHT = 160
#: Factor of one step of *Taller channels* / *Shorter channels* and of a wheel notch with Alt.
CHANNEL_HEIGHT_STEP = 1.25


@dataclass
class AnnotationHover:
    """The annotation under the pointer and how its value is composed."""

    group: Any  # sigrok.provider.AnnotationGroup
    segment: Any  # sigrok.engine.AnnotationSegment
    #: Levels of the channels at the read point; only for an entry that is a value of its own
    composition: Any = None  # sigrok.composition.Composition
    #: The entries of other rows that belong to it (``sigrok.links.LinkedSegment``)
    links: list = field(default_factory=list)
    #: The values it is made of (e.g. the bus cycles of an instruction), in time order
    parts: list = field(default_factory=list)

    @property
    def linked_ids(self) -> set[int]:
        return {id(link.segment) for link in self.links}


#: Names of the measurement cursors
CURSORS = ("A", "B")


@dataclass(eq=False)
class Bookmark:
    """A named position in the capture."""

    sample: int
    name: str = ""


@dataclass
class SearchHits:
    """Result of the search panel: positions (and optional end positions) of the matches."""

    starts: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    ends: Optional[np.ndarray] = None
    #: Index of the selected match, -1: none
    current: int = -1

    def __len__(self) -> int:
        return len(self.starts)


class CaptureViewModel(QObject):
    """Capture, viewport, regions and marker shared by all capture widgets."""

    capture_changed = Signal()
    view_changed = Signal()
    regions_changed = Signal()
    marker_changed = Signal()
    channels_changed = Signal()
    annotations_changed = Signal()
    hover_changed = Signal()
    channel_height_changed = Signal()
    #: the samples of a live capture grew (:meth:`extend_live`)
    samples_appended = Signal()
    cursors_changed = Signal()
    bookmarks_changed = Signal()
    search_changed = Signal()
    buses_changed = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hover: Optional[AnnotationHover] = None
        self._channel_height = DEFAULT_CHANNEL_HEIGHT
        #: ``id()`` of the pinned channels.
        self._pinned: set[int] = set()
        self._session: Optional[CaptureSession] = None
        self._live = False
        self._transitions: list[ChannelTransitions] = []
        self._first_sample = 0
        self._visible_samples = 200
        self._user_marker: Optional[int] = None
        self._regions: list[SampleRegion] = []
        self._annotation_groups: list = []
        self._cursors: dict[str, Optional[int]] = dict.fromkeys(CURSORS)
        self._bookmarks: list[Bookmark] = []
        self._search = SearchHits()
        #: Values of the buses by ``id()`` of their definition, computed when first drawn
        self._bus_values: dict[int, np.ndarray] = {}

    # ---------------------------------------------------------------- capture
    @property
    def session(self) -> Optional[CaptureSession]:
        return self._session

    @property
    def channels(self) -> list[AnalyzerChannel]:
        return list(self._session.capture_channels) if self._session else []

    @property
    def visible_channels(self) -> list[AnalyzerChannel]:
        return [channel for channel in self.channels if not channel.hidden]

    # --------------------------------------------------------- channel height
    @property
    def channel_height(self) -> int:
        """Minimum height of a channel row; smaller rows fit more channels on the screen."""
        return self._channel_height

    def set_channel_height(self, height: int) -> None:
        height = int(max(SMALLEST_CHANNEL_HEIGHT, min(int(height), LARGEST_CHANNEL_HEIGHT)))
        if height == self._channel_height:
            return
        self._channel_height = height
        self.channel_height_changed.emit()

    def zoom_channels(self, factor: float) -> None:
        """Multiply the channel height by ``factor`` (at least one pixel per step)."""
        target = int(round(self._channel_height * factor))
        if target == self._channel_height:
            target += 1 if factor > 1 else -1
        self.set_channel_height(target)

    # ----------------------------------------------------------------- pinned
    def is_pinned(self, channel: AnalyzerChannel) -> bool:
        return id(channel) in self._pinned

    def set_pinned(self, channel: AnalyzerChannel, pinned: bool) -> None:
        """Pinned channels stay at the top of the waveform while the others scroll."""
        if pinned == self.is_pinned(channel):
            return
        if pinned:
            self._pinned.add(id(channel))
        else:
            self._pinned.discard(id(channel))
        self.channels_changed.emit()

    def unpin_all(self) -> None:
        if self._pinned:
            self._pinned.clear()
            self.channels_changed.emit()

    def section_channels(self, section: str = "all") -> list[AnalyzerChannel]:
        """Visible channels of a part of the waveform: ``all``, ``pinned`` or ``scrolling``."""
        channels = self.visible_channels
        if section == "pinned":
            return [channel for channel in channels if self.is_pinned(channel)]
        if section == "scrolling":
            return [channel for channel in channels if not self.is_pinned(channel)]
        return channels

    @property
    def transitions(self) -> list[ChannelTransitions]:
        return self._transitions

    @property
    def sample_count(self) -> int:
        return self._session.sample_count() if self._session else 0

    @property
    def frequency(self) -> int:
        return self._session.frequency if self._session else 1

    @property
    def pre_trigger_samples(self) -> int:
        return self._session.pre_trigger_samples if self._session else 0

    @property
    def is_live(self) -> bool:
        """The session is a capture still streaming in; it is replaced when it completes."""
        return self._live

    def set_session(
        self, session: Optional[CaptureSession], live: bool = False, first_sample: int = 0
    ) -> None:
        """``live``: a capture still streaming in (see :meth:`extend_live`), whose sample 0 is
        at ``first_sample`` of the stream."""
        if session is not self._session:
            self._pinned.clear()
        self._session = session
        self._live = live and session is not None
        self.rebuild_transitions(first_sample if self._live else 0)
        self._user_marker = None
        self._bus_values.clear()
        self._cursors = dict.fromkeys(CURSORS)
        self._bookmarks = []
        self._search = SearchHits()
        self.set_hover(None)
        self.capture_changed.emit()
        self.marker_changed.emit()
        self.cursors_changed.emit()
        self.bookmarks_changed.emit()
        self.search_changed.emit()
        self.buses_changed.emit()

    @property
    def on_disk(self) -> bool:
        """The samples are memory-mapped files (a stream recorded to disk)."""
        return self._session is not None and any(
            is_on_disk(channel.samples) for channel in self._session.capture_channels
        )

    def finish_live(self, session: CaptureSession, first_sample: int = 0) -> bool:
        """Shows the completed capture of the live session, keeping its edge index.

        The index then only grows by the samples that arrived after the last update, instead of
        being built again over the whole capture. ``False`` when it does not fit (then call
        :meth:`set_session`).
        """
        if not self._live or self._session is None:
            return False
        numbers = [channel.channel_number for channel in session.capture_channels]
        if numbers != [channel.channel_number for channel in self._session.capture_channels]:
            return False
        for channel, transitions in zip(session.capture_channels, self._transitions):
            dropped = max(first_sample - transitions.origin, 0)
            if channel.samples is None or len(channel.samples) < transitions.sample_count - dropped:
                return False
        for channel, transitions in zip(session.capture_channels, self._transitions):
            transitions.slide_to(first_sample)
            transitions.extend(channel.samples, len(channel.samples))
        self._pinned.clear()
        self._session = session
        self._live = False
        self._user_marker = None
        self._bus_values.clear()
        self.set_hover(None)
        self.capture_changed.emit()
        self.marker_changed.emit()
        return True

    def extend_live(self, first_sample: int = 0) -> None:
        """Indexes the samples the channels of the live session got since the last call.

        ``first_sample`` is the stream position of sample 0: an endless stream drops its oldest
        samples, and its channels hold only the latest ones.
        """
        if not self._live:
            return
        for channel, transitions in zip(self._session.capture_channels, self._transitions):
            if channel.samples is not None:
                transitions.slide_to(first_sample)
                transitions.extend(channel.samples, len(channel.samples))
        self.samples_appended.emit()

    def rebuild_transitions(self, origin: int = 0) -> None:
        """Re-index the channels; call after the samples were modified."""
        if self._session is None:
            self._transitions = []
            return
        self._transitions = build_transitions(
            self._session.capture_channels, self._session.frequency, origin
        )

    def transitions_for(self, channel: AnalyzerChannel) -> Optional[ChannelTransitions]:
        """Edge index of ``channel``, looked up by identity."""
        if self._session is None:
            return None
        for index, candidate in enumerate(self._session.capture_channels):
            if candidate is channel:
                return self._transitions[index] if index < len(self._transitions) else None
        return None

    def notify_channels_changed(self) -> None:
        self.channels_changed.emit()

    def notify_capture_changed(self) -> None:
        self._bus_values.clear()
        self.capture_changed.emit()

    # --------------------------------------------------------------- viewport
    @property
    def first_sample(self) -> int:
        return self._first_sample

    @property
    def visible_samples(self) -> int:
        return self._visible_samples

    @property
    def last_sample(self) -> int:
        return self._first_sample + self._visible_samples - 1

    def max_visible_samples(self) -> int:
        return max(self.sample_count, MIN_VISIBLE_SAMPLES)

    def set_view(self, first_sample: int, visible_samples: int) -> None:
        total = self.sample_count
        visible_samples = int(max(MIN_VISIBLE_SAMPLES, visible_samples))
        if total:
            visible_samples = min(visible_samples, max(total, MIN_VISIBLE_SAMPLES))
            first_sample = int(max(0, min(first_sample, max(total - visible_samples, 0))))
        else:
            first_sample = max(0, int(first_sample))

        if first_sample == self._first_sample and visible_samples == self._visible_samples:
            return

        self._first_sample = first_sample
        self._visible_samples = visible_samples
        self.view_changed.emit()

    def scroll_to(self, first_sample: int) -> None:
        self.set_view(first_sample, self._visible_samples)

    def scroll_by(self, samples: int) -> None:
        self.set_view(self._first_sample + samples, self._visible_samples)

    def zoom(self, factor: float, anchor_sample: Optional[float] = None) -> None:
        """Zoom keeping ``anchor_sample`` (a sample position) under the cursor."""
        if anchor_sample is None:
            anchor_sample = self._first_sample + self._visible_samples / 2.0

        new_visible = int(round(self._visible_samples * factor))
        new_visible = max(MIN_VISIBLE_SAMPLES, min(new_visible, self.max_visible_samples()))
        if new_visible == self._visible_samples:
            return

        ratio = (anchor_sample - self._first_sample) / max(self._visible_samples, 1)
        first = int(round(anchor_sample - ratio * new_visible))
        self.set_view(first, new_visible)

    def zoom_to_fit(self) -> None:
        self.set_view(0, self.max_visible_samples())

    def center_on(self, sample: int) -> None:
        self.set_view(int(sample - self._visible_samples // 2), self._visible_samples)

    def sample_at(self, x: float, width: float) -> int:
        """Sample under the horizontal position ``x`` of a ``width`` wide widget."""
        if width <= 0:
            return self._first_sample
        return int(x / (width / self._visible_samples)) + self._first_sample

    # ----------------------------------------------------------------- marker
    @property
    def user_marker(self) -> Optional[int]:
        return self._user_marker

    def set_user_marker(self, sample: Optional[int]) -> None:
        if sample is not None and (sample < 0 or sample > self.sample_count):
            sample = None
        if sample == self._user_marker:
            return
        self._user_marker = sample
        self.marker_changed.emit()

    # ---------------------------------------------------------------- regions
    @property
    def regions(self) -> list[SampleRegion]:
        return list(self._regions)

    def add_region(self, region: SampleRegion) -> None:
        self._regions.append(region)
        self.regions_changed.emit()

    def add_regions(self, regions: Iterable[SampleRegion]) -> None:
        added = list(regions)
        if not added:
            return
        self._regions.extend(added)
        self.regions_changed.emit()

    def remove_region(self, region: SampleRegion) -> bool:
        if region not in self._regions:
            return False
        self._regions.remove(region)
        self.regions_changed.emit()
        return True

    def set_regions(self, regions: Sequence[SampleRegion]) -> None:
        self._regions = list(regions)
        self.regions_changed.emit()

    def clear_regions(self) -> None:
        if not self._regions:
            return
        self._regions.clear()
        self.regions_changed.emit()

    def region_at(self, sample: int) -> Optional[SampleRegion]:
        for region in self._regions:
            if region.contains(sample):
                return region
        return None

    def regions_in_view(self) -> list[SampleRegion]:
        first, last = self._first_sample, self.last_sample
        return [region for region in self._regions if region.end >= first and region.start <= last]

    # ------------------------------------------------------------ annotations
    @property
    def annotation_groups(self) -> list:
        return self._annotation_groups

    def set_annotation_groups(self, groups: Sequence) -> None:
        self._annotation_groups = list(groups)
        self.set_hover(None)
        self.annotations_changed.emit()

    @property
    def hover(self) -> Optional[AnnotationHover]:
        return self._hover

    def set_hover(self, hover: Optional[AnnotationHover]) -> None:
        """Mark an annotation in every view (``None`` clears the mark)."""
        current = self._hover
        if hover is None and current is None:
            return
        if hover is not None and current is not None and hover.segment is current.segment:
            return
        self._hover = hover
        self.hover_changed.emit()

    # ---------------------------------------------------------------- cursors
    def cursor(self, name: str) -> Optional[int]:
        return self._cursors.get(name)

    def set_cursor(self, name: str, sample: Optional[int]) -> None:
        if sample is not None:
            sample = int(min(max(sample, 0), max(self.sample_count - 1, 0)))
        if self._cursors.get(name) == sample:
            return
        self._cursors[name] = sample
        self.cursors_changed.emit()

    def clear_cursors(self) -> None:
        if any(value is not None for value in self._cursors.values()):
            self._cursors = dict.fromkeys(CURSORS)
            self.cursors_changed.emit()

    def cursor_delta(self) -> Optional[int]:
        """Samples from cursor A to cursor B (``None`` unless both are placed)."""
        a, b = self._cursors["A"], self._cursors["B"]
        return None if a is None or b is None else b - a

    def time_of(self, sample: float) -> float:
        """Seconds of ``sample`` from the trigger."""
        return (sample - self.pre_trigger_samples) / max(self.frequency, 1)

    # -------------------------------------------------------------- bookmarks
    @property
    def bookmarks(self) -> list[Bookmark]:
        return sorted(self._bookmarks, key=lambda bookmark: bookmark.sample)

    def add_bookmark(self, sample: int, name: str = "") -> Bookmark:
        bookmark = Bookmark(int(sample), name or f"Marker {len(self._bookmarks) + 1}")
        self._bookmarks.append(bookmark)
        self.bookmarks_changed.emit()
        return bookmark

    def remove_bookmark(self, bookmark: Bookmark) -> None:
        if bookmark in self._bookmarks:
            self._bookmarks.remove(bookmark)
            self.bookmarks_changed.emit()

    def rename_bookmark(self, bookmark: Bookmark, name: str) -> None:
        bookmark.name = name
        self.bookmarks_changed.emit()

    def clear_bookmarks(self) -> None:
        if self._bookmarks:
            self._bookmarks.clear()
            self.bookmarks_changed.emit()

    # ----------------------------------------------------------------- search
    @property
    def search_hits(self) -> SearchHits:
        return self._search

    def set_search_hits(self, starts: np.ndarray, ends: Optional[np.ndarray] = None) -> None:
        self._search = SearchHits(np.asarray(starts, dtype=np.int64), ends, -1)
        self.search_changed.emit()

    def select_search_hit(self, index: int) -> None:
        """Selects match ``index`` and brings it into view."""
        hits = self._search
        if not len(hits):
            return
        index %= len(hits)
        hits.current = index
        start = int(hits.starts[index])
        end = int(hits.ends[index]) if hits.ends is not None else start
        if end - start >= self._visible_samples:
            self.set_view(start - (end - start) // 10, int((end - start) * 1.2) + 1)
        elif not self._first_sample <= start <= self.last_sample or not end <= self.last_sample:
            self.center_on((start + end) // 2)
        self.search_changed.emit()

    # ------------------------------------------------------------------ buses
    @property
    def buses(self) -> list[BusDefinition]:
        return list(self._session.buses) if self._session else []

    def set_buses(self, buses: Sequence[BusDefinition]) -> None:
        if self._session is None:
            return
        self._session.buses = list(buses)
        self._bus_values.clear()
        self.buses_changed.emit()

    def bus_values(self, bus: BusDefinition) -> np.ndarray:
        values = self._bus_values.get(id(bus))
        if values is None or len(values) != self.sample_count:
            from ..core.buses import bus_values

            values = bus_values(self._session, bus)
            self._bus_values[id(bus)] = values
        return values
