# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
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

from ..core.analysis import ChannelTransitions
from ..core.overview import Overview
from ..core.regions import SampleRegion
from ..core.sample_store import is_on_disk
from ..driver.models import AnalogChannel, AnalyzerChannel, BusDefinition, CaptureSession

MIN_VISIBLE_SAMPLES = 4
def _dense(pixels: int) -> int:
    from .theme import scaled

    return scaled(pixels)


#: Height of a channel row in pixels: the rows grow to fill the view, this is their minimum (40 with
#: the standard text of 10 px, 48 with 12 px).
DEFAULT_CHANNEL_HEIGHT = _dense(48)
SMALLEST_CHANNEL_HEIGHT = 18
LARGEST_CHANNEL_HEIGHT = 160
#: Factor of one step of *Taller channels* / *Shorter channels* and of a wheel notch with Alt.
CHANNEL_HEIGHT_STEP = 1.25
#: Height of an analog track in pixels
DEFAULT_ANALOG_HEIGHT = 96
SMALLEST_ANALOG_HEIGHT = 40
LARGEST_ANALOG_HEIGHT = 480
#: Analog channels longer than this get an overview for drawing them zoomed out
OVERVIEW_FROM = 1 << 16


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
    #: tiles of a progressively transferred capture arrived (``progressive_complete`` after the last)
    tiles_arrived = Signal()
    analog_height_changed = Signal()
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
        #: id of a bus -> (its values, the runs of them)
        self._bus_runs: dict[int, tuple[np.ndarray, tuple[np.ndarray, np.ndarray]]] = {}
        #: id of a sample array -> (the array, its index): what :meth:`rebuild_transitions` keeps
        self._indexed: dict[int, tuple[np.ndarray, ChannelTransitions]] = {}
        self._first_sample = 0
        self._visible_samples = 200
        self._user_marker: Optional[int] = None
        #: the samples selected on the ruler (first, last), shown over every track
        self._selection: Optional[tuple[int, int]] = None
        self._regions: list[SampleRegion] = []
        self._annotation_groups: list = []
        self._cursors: dict[str, Optional[int]] = dict.fromkeys(CURSORS)
        self._bookmarks: list[Bookmark] = []
        self._search = SearchHits()
        #: Values of the buses by ``id()`` of their definition, computed when first drawn
        #: id of a bus -> (the sample arrays of its channels, its value at every sample)
        self._bus_values: dict[int, tuple[list, np.ndarray]] = {}
        self._analog_height = DEFAULT_ANALOG_HEIGHT
        #: Overviews of analog channels by ``id()``, made when first drawn zoomed out
        self._analog_overviews: dict[int, Overview] = {}
        #: Value range (min, max in the channel's unit) of analog channels by ``id()``
        self._analog_ranges: dict[int, tuple[float, float]] = {}
        #: State captures: show the states on their real time instead of one per state

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

    # ---------------------------------------------------------------- analog
    @property
    def analog_channels(self) -> list[AnalogChannel]:
        return list(self._session.analog_channels) if self._session else []

    @property
    def visible_analog(self) -> list[AnalogChannel]:
        return [channel for channel in self.analog_channels if not channel.hidden]

    @property
    def analog_height(self) -> int:
        return self._analog_height

    def set_analog_height(self, height: int) -> None:
        height = int(max(SMALLEST_ANALOG_HEIGHT, min(int(height), LARGEST_ANALOG_HEIGHT)))
        if height != self._analog_height:
            self._analog_height = height
            self.analog_height_changed.emit()

    def analog_index(self, sample: float, channel: AnalogChannel) -> float:
        """The sample of ``channel`` at sample ``sample`` of the session (its own rate)."""
        if channel.rate and self._session is not None and channel.rate != self._session.frequency:
            return sample * channel.rate / self._session.frequency
        return sample

    def analog_overview(self, channel: AnalogChannel) -> Optional[Overview]:
        """The min/max overview of ``channel`` (from a progressive transfer, or built from its samples)."""
        progressive = self.progressive
        if progressive is not None:
            return progressive.overviews.get(("a", channel.channel_number))
        overview = self._analog_overviews.get(id(channel))
        if overview is None and channel.raw is not None and len(channel.raw) >= OVERVIEW_FROM:
            overview = Overview.build(np.asarray(channel.raw))
            self._analog_overviews[id(channel)] = overview
        return overview

    def analog_range(self, channel: AnalogChannel) -> tuple[float, float]:
        """Lowest and highest value of ``channel`` (in its unit), for the scale of its track."""
        cached = self._analog_ranges.get(id(channel))
        if cached is not None:
            return cached
        overview = self.analog_overview(channel)
        if overview is not None and overview.levels:
            low, high = overview.levels[-1][0].min(), overview.levels[-1][1].max()
        elif channel.raw is not None and len(channel.raw):
            low, high = np.min(channel.raw), np.max(channel.raw)
        else:
            return (-1.0, 1.0)
        low, high = float(low) * channel.scale + channel.offset, float(high) * channel.scale + channel.offset
        if high - low < 1e-9:
            low, high = low - 0.5, high + 0.5
        margin = (high - low) * 0.06
        value = (low - margin, high + margin)
        if self.progressive is None:
            self._analog_ranges[id(channel)] = value
        return value

    def digital_overview(self, channel: AnalyzerChannel) -> Optional[Overview]:
        progressive = self.progressive
        return progressive.overviews.get(("d", channel.channel_number)) if progressive is not None else None

    # ----------------------------------------------------------- progressive
    @property
    def progressive(self):
        """The progressive transfer of the capture while it is incomplete, else ``None``."""
        progressive = getattr(self._session, "progressive", None) if self._session is not None else None
        return progressive if progressive is not None and not progressive.complete else None

    def tiles_loaded(self) -> None:
        """Parts of the progressive capture arrived: redraw, and index it once complete."""
        session = self._session
        if session is None:
            return
        progressive = getattr(session, "progressive", None)
        if progressive is not None and progressive.complete:
            self.rebuild_transitions(force=True)  # the arrays were filled in place
            self._analog_overviews.clear()
            self._analog_ranges.clear()
            self._bus_values.clear()
            self.capture_changed.emit()
        self.tiles_arrived.emit()
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

    def pinned_numbers(self) -> list[int]:
        """Numbers of the pinned channels, in the order of the channels."""
        return [channel.channel_number for channel in self.channels if self.is_pinned(channel)]

    def set_pinned_numbers(self, numbers: Iterable[int]) -> None:
        wanted = {int(number) for number in numbers}
        pinned = {id(channel) for channel in self.channels if channel.channel_number in wanted}
        if pinned != self._pinned:
            self._pinned = pinned
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
        other = session is not self._session
        if other:
            self._pinned.clear()
            self._analog_overviews.clear()
            self._analog_ranges.clear()
        self._session = session
        self._live = live and session is not None
        # (another capture is indexed from scratch: a device may fill the arrays it used before)
        self.rebuild_transitions(first_sample if self._live else 0, force=other)
        self._user_marker = None
        self._selection = None
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
        self._selection = None
        self._bus_values.clear()
        self.set_hover(None)
        self.capture_changed.emit()
        self.marker_changed.emit()
        return True

    def notify_analog_changed(self) -> None:
        """The analog samples changed (live, derived channels): forget their overviews and ranges."""
        self._analog_overviews.clear()
        self._analog_ranges.clear()
        self.samples_appended.emit()

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

    def rebuild_transitions(self, origin: int = 0, force: bool = False) -> None:
        """Index the channels; call after the samples or the order of the channels changed.

        Edits replace the sample array of a channel, they never change it: the index of an
        array that is still the one it was built from is kept, so reordering the channels,
        an undo or an edit of some channels only indexes what is new (a pass over every sample
        of every channel takes seconds for a large capture). ``force``: the arrays were filled
        in place (a transfer that completed) – index all of them.
        """
        if self._session is None or self.progressive is not None:
            # A progressive capture is indexed once all of it arrived (drawn from overviews before).
            self._transitions = []
            self._indexed = {}
            return
        frequency = max(int(self._session.frequency), 1)
        known = {} if force or origin or self._live else self._indexed
        indexed: dict[int, tuple[np.ndarray, ChannelTransitions]] = {}
        transitions = []
        for channel in self._session.capture_channels:
            samples = channel.samples
            entry = known.get(id(samples)) if samples is not None else None
            if entry is not None and entry[0] is samples and entry[1].frequency == frequency \
                    and entry[1].sample_count == len(samples):
                index = entry[1]
            else:
                index = ChannelTransitions(samples, frequency, origin)
            transitions.append(index)
            if samples is not None and not origin and not self._live:
                indexed[id(samples)] = (samples, index)
        self._transitions = transitions
        self._indexed = indexed

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

    def move_channel(self, channel: AnalyzerChannel, index: int) -> bool:
        """Show ``channel`` at ``index`` among the channels (the order is saved with the capture)."""
        if self._session is None or channel not in self._session.capture_channels:
            return False
        channels = self._session.capture_channels
        old = channels.index(channel)
        index = max(0, min(int(index), len(channels) - 1))
        if index == old:
            return False
        channels.insert(index, channels.pop(old))
        self.rebuild_transitions()
        self.channels_changed.emit()
        self.capture_changed.emit()
        return True

    def notify_capture_changed(self) -> None:
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

    @property
    def selection(self) -> Optional[tuple[int, int]]:
        """The samples selected on the ruler: ``(first, last)``, both included; ``None``: none."""
        return self._selection

    def set_selection(self, first: Optional[int], last: Optional[int] = None) -> None:
        selection = None if first is None else tuple(sorted((int(first), int(last if last is not None else first))))
        if selection == self._selection:
            return
        self._selection = selection
        self.marker_changed.emit()

    def zoom_to_selection(self) -> bool:
        """Show exactly the selected samples (with a little room on both sides)."""
        if self._selection is None:
            return False
        first, last = self._selection
        count = last - first + 1
        margin = max(count // 20, 1)
        self.set_view(max(first - margin, 0), count + 2 * margin)
        return True

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
        """Seconds of ``sample`` from the trigger (state captures with times: from the first state)."""
        times = self.state_times
        if times is not None and len(times):
            index = int(min(max(sample, 0), len(times) - 1))
            return float(times[index]) / 1e6
        return (sample - self.pre_trigger_samples) / max(self.frequency, 1)

    @property
    def state_times(self) -> Optional[np.ndarray]:
        """Microseconds of every state of a state capture, ``None`` for timing captures."""
        session = self._session
        return None if session is None else session.state_times

    # -------------------------------------------------------------- bookmarks
    @property
    def bookmarks(self) -> list[Bookmark]:
        return sorted(self._bookmarks, key=lambda bookmark: bookmark.sample)

    def add_bookmark(self, sample: int, name: str = "") -> Bookmark:
        bookmark = Bookmark(int(sample), name or f"Marker {len(self._bookmarks) + 1}")
        self._bookmarks.append(bookmark)
        self.bookmarks_changed.emit()
        return bookmark

    def set_bookmarks(self, bookmarks: Iterable[tuple[int, str]]) -> None:
        """Replace the markers (as a capture file holds them: sample and name)."""
        self._bookmarks = [Bookmark(int(sample), str(name)) for sample, name in bookmarks]
        self.bookmarks_changed.emit()

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

    def bus_run_index(self, bus: BusDefinition) -> tuple[np.ndarray, np.ndarray]:
        """First sample and value of every run of equal values of ``bus`` over the whole capture
        (built once; the rows of the buses are drawn from it at any zoom)."""
        entry = self._bus_runs.get(id(bus))
        values = self.bus_values(bus)
        if entry is None or entry[0] is not values:
            from ..core.buses import bus_runs

            entry = (values, bus_runs(values, 0, len(values)))
            self._bus_runs[id(bus)] = entry
        return entry[1]

    def bus_values(self, bus: BusDefinition) -> np.ndarray:
        """The value of ``bus`` at every sample, computed once for the sample arrays of its
        channels as they are (an edit replaces the arrays it changes: then it is computed again)."""
        session = self._session
        arrays = {channel.channel_number: channel.samples for channel in session.capture_channels}
        sources = [arrays.get(number) for number in bus.channels[:64]]
        entry = self._bus_values.get(id(bus))
        if entry is None or len(entry[1]) != self.sample_count or len(entry[0]) != len(sources) \
                or any(old is not new for old, new in zip(entry[0], sources)):
            from ..core.buses import bus_values

            entry = (sources, bus_values(session, bus))
            self._bus_values[id(bus)] = entry
        return entry[1]
