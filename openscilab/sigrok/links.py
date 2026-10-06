# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Annotations that belong together across rows: the parts of an entry and the entry around it.

An instruction of a disassembly consists of bus cycles in another row; a bus cycle belongs to
an instruction. Hovering either marks the other, so the rows read as one decoded structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

#: Relations of a linked annotation to the hovered one
PART = "part"
WHOLE = "whole"
#: Rows with more parts than this inside the hovered entry are not marked (e.g. every bus cycle
#: inside a memory region spanning the capture)
MAX_PARTS = 256
#: An entry around the hovered one is only marked when it is at most this many times as long
#: (an instruction around a bus cycle, not a memory region around everything)
MAX_WHOLE_RATIO = 32
#: Entries before the hovered one that are checked for one around it
LOOKBEHIND = 64


@dataclass
class LinkedSegment:
    group: Any  # sigrok.provider.AnnotationGroup
    annotation: Any  # sigrok.engine.Annotation (the row)
    segment: Any  # sigrok.engine.AnnotationSegment
    relation: str


def segment_index(annotation) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The segments of a row (sorted by first sample) as arrays, built once per row: their first
    samples, their last samples, the furthest last sample of all segments up to each one (to
    find where the segments reaching into a range begin), and their type ids."""
    segments = annotation.segments
    cached = getattr(annotation, "_segment_index", None)
    if cached is None or len(cached[0]) != len(segments):
        starts = np.array([segment.first_sample for segment in segments], dtype=np.int64)
        ends = np.array([max(segment.last_sample, segment.first_sample) for segment in segments], dtype=np.int64)
        reach = np.maximum.accumulate(ends) if len(ends) else ends
        types = np.array([segment.type_id for segment in segments], dtype=np.int64)
        cached = (starts, ends, reach, types)
        annotation._segment_index = cached
    return cached


def _index(annotation) -> tuple[np.ndarray, np.ndarray]:
    """First and last samples of the segments of a row (sorted by first sample), cached."""
    return segment_index(annotation)[:2]


def linked_segments(groups: Sequence, hovered) -> list[LinkedSegment]:
    """The parts of ``hovered`` and, per row, the shortest entry around it, in every row."""
    first = hovered.first_sample
    last = max(hovered.last_sample, first)
    length = max(last - first, 1)
    links: list[LinkedSegment] = []
    for group in groups:
        for annotation in group.annotations:
            segments = annotation.segments
            if not segments:
                continue
            starts, ends = _index(annotation)

            low = int(np.searchsorted(starts, first, side="left"))
            high = int(np.searchsorted(starts, last, side="right"))
            # (as arrays: a long entry – a frame, a memory region – has thousands of entries
            # of other rows inside it, looked at on every move of the pointer)
            inside = (np.flatnonzero(ends[low:high] <= last) + low) if last > first else np.zeros(0, np.int64)
            if len(inside) <= MAX_PARTS + 1:
                parts = [int(index) for index in inside if segments[int(index)] is not hovered]
                if len(parts) <= MAX_PARTS:
                    links += [LinkedSegment(group, annotation, segments[index], PART) for index in parts]

            whole = None
            for index in range(min(high, len(segments)) - 1, max(low - LOOKBEHIND, -1), -1):
                segment = segments[index]
                if segment is hovered or starts[index] > first or ends[index] < last:
                    continue
                span = ends[index] - starts[index]
                if span <= last - first or span > MAX_WHOLE_RATIO * length:
                    continue
                if whole is None or span < whole[1]:
                    whole = (segment, span)
            if whole is not None:
                links.append(LinkedSegment(group, annotation, whole[0], WHOLE))
    return links


def read_parts(links: Sequence[LinkedSegment], group) -> list:
    """The values the hovered entry is made of (the bus cycles of an instruction), in time order:
    the parts in the row of its decoder that has the most of them, at least two. An entry with
    fewer is a value of its own, even when another row describes the same span."""
    rows: dict[int, list] = {}
    for link in links:
        if link.relation == PART and link.group is group:
            rows.setdefault(id(link.annotation), []).append(link.segment)
    parts = max(rows.values(), key=len, default=[])
    if len(parts) < 2:
        return []
    return sorted(parts, key=lambda segment: segment.first_sample)
