# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
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


def _index(annotation) -> tuple[np.ndarray, np.ndarray]:
    """First and last samples of the segments of a row (sorted by first sample), cached."""
    segments = annotation.segments
    cached = getattr(annotation, "_link_index", None)
    if cached is None or len(cached[0]) != len(segments):
        starts = np.array([segment.first_sample for segment in segments], dtype=np.int64)
        ends = np.array([max(segment.last_sample, segment.first_sample) for segment in segments], dtype=np.int64)
        cached = (starts, ends)
        annotation._link_index = cached
    return cached


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
            inside = [index for index in range(low, high) if ends[index] <= last and segments[index] is not hovered]
            if len(inside) <= MAX_PARTS and last > first:
                links += [LinkedSegment(group, annotation, segments[index], PART) for index in inside]

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
