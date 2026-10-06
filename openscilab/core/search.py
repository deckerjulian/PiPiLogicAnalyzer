# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Searching a capture for bus values and decoder annotations.

The searches return the sample positions of every match at once (sorted), so
"next" and "previous" are a binary search with :func:`next_match`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np

#: Operators of :func:`find_bus_values`
BUS_OPERATORS = ("==", "!=", "<", "<=", ">", ">=", "in")


def _value_runs(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(starts, values)`` of the runs of equal values."""
    values = np.asarray(values)
    if values.size == 0:
        return np.zeros(0, dtype=np.int64), values
    starts = np.flatnonzero(values[1:] != values[:-1]) + 1
    starts = np.concatenate((np.zeros(1, dtype=np.int64), starts.astype(np.int64)))
    return starts, values[starts]


def find_bus_values(values: np.ndarray, op: str, operand: int, operand2: Optional[int] = None) -> np.ndarray:
    """Start samples of the runs whose value satisfies ``value <op> operand``.

    ``op`` is one of :data:`BUS_OPERATORS`; ``in`` matches ``operand..operand2``
    (inclusive). Every matching run is reported once, at its first sample.
    """
    if op not in BUS_OPERATORS:
        raise ValueError(f"Unknown operator: {op}")
    if op == "in" and operand2 is None:
        raise ValueError("'in' needs two operands")
    starts, runs = _value_runs(values)
    # Python ints compare exactly with any unsigned dtype (also when out of its range).
    operand = int(operand)
    if op == "==":
        mask = runs == operand
    elif op == "!=":
        mask = runs != operand
    elif op == "<":
        mask = runs < operand
    elif op == "<=":
        mask = runs <= operand
    elif op == ">":
        mask = runs > operand
    elif op == ">=":
        mask = runs >= operand
    else:
        low, high = sorted((operand, int(operand2)))
        mask = (runs >= low) & (runs <= high)
    return starts[mask]


@dataclass(frozen=True)
class AnnotationMatch:
    """An annotation segment whose text matched a search."""

    first_sample: int
    last_sample: int
    #: The value of the segment that matched
    text: str
    decoder: str
    row: str


def find_annotations(
    groups: Iterable,
    text: str,
    regex: bool = False,
    case_sensitive: bool = False,
    decoder: Optional[str] = None,
    row: Optional[str] = None,
) -> list[AnnotationMatch]:
    """Annotation segments of ``groups`` (``AnnotationGroup``) with a value containing
    ``text`` (or matching the regular expression ``text``), sorted by first sample.

    ``decoder`` and ``row`` limit the search to the groups / rows of that name (any
    case). An empty ``text`` matches every segment. Raises ``re.error`` for an
    invalid expression.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(text if regex else re.escape(text), flags)
    decoder_key = decoder.casefold() if decoder else None
    row_key = row.casefold() if row else None
    matches: list[AnnotationMatch] = []
    for group in groups:
        if decoder_key is not None and group.decoder_name.casefold() != decoder_key:
            continue
        for annotation in group.annotations:
            if row_key is not None and annotation.name.casefold() != row_key:
                continue
            for segment in annotation.segments:
                for value in segment.values:
                    if pattern.search(value):
                        matches.append(
                            AnnotationMatch(
                                segment.first_sample, segment.last_sample, value, group.decoder_name, annotation.name
                            )
                        )
                        break
    matches.sort(key=lambda match: (match.first_sample, match.last_sample))
    return matches


def match_positions(matches: list[AnnotationMatch]) -> np.ndarray:
    """First samples of ``matches`` (for :func:`next_match`)."""
    return np.fromiter((m.first_sample for m in matches), dtype=np.int64, count=len(matches))


def next_match(positions: np.ndarray, sample: int, forward: bool = True, wrap: bool = True) -> Optional[int]:
    """The first of the sorted ``positions`` after ``sample`` (``forward``) or the last
    before it; ``wrap`` continues at the other end. ``None`` without a match."""
    count = len(positions)
    if count == 0:
        return None
    if forward:
        index = int(np.searchsorted(positions, sample, side="right"))
        if index < count:
            return int(positions[index])
        return int(positions[0]) if wrap else None
    index = int(np.searchsorted(positions, sample, side="left")) - 1
    if index >= 0:
        return int(positions[index])
    return int(positions[-1]) if wrap else None
