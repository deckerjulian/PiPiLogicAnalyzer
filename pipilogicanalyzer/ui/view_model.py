# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
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

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from ..core.analysis import ChannelTransitions, build_transitions
from ..core.regions import SampleRegion
from ..driver.models import AnalyzerChannel, CaptureSession

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
    composition: Any = None  # sigrok.composition.Composition


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

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hover: Optional[AnnotationHover] = None
        self._channel_height = DEFAULT_CHANNEL_HEIGHT
        #: ``id()`` of the pinned channels.
        self._pinned: set[int] = set()
        self._session: Optional[CaptureSession] = None
        self._transitions: list[ChannelTransitions] = []
        self._first_sample = 0
        self._visible_samples = 200
        self._user_marker: Optional[int] = None
        self._regions: list[SampleRegion] = []
        self._annotation_groups: list = []

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

    def set_session(self, session: Optional[CaptureSession]) -> None:
        if session is not self._session:
            self._pinned.clear()
        self._session = session
        self.rebuild_transitions()
        self._user_marker = None
        self.set_hover(None)
        self.capture_changed.emit()
        self.marker_changed.emit()

    def rebuild_transitions(self) -> None:
        """Re-index the channels; call after the samples were modified."""
        if self._session is None:
            self._transitions = []
            return
        self._transitions = build_transitions(
            self._session.capture_channels, self._session.frequency
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
