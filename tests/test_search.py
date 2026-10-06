"""Searching bus values and decoder annotations."""

from __future__ import annotations

import re
import time

import numpy as np
import pytest

from openscilab.core.search import (
    AnnotationMatch,
    find_annotations,
    find_bus_values,
    match_positions,
    next_match,
)
from openscilab.sigrok.engine import Annotation, AnnotationSegment
from openscilab.sigrok.provider import AnnotationGroup, DecoderInstance

VALUES = np.array([1, 1, 5, 5, 2, 7, 7, 1, 9], dtype=np.uint8)


@pytest.mark.parametrize(
    "op,operand,operand2,expected",
    [
        ("==", 1, None, [0, 7]),
        ("==", 3, None, []),
        ("!=", 1, None, [2, 4, 5, 8]),
        ("<", 5, None, [0, 4, 7]),
        ("<=", 5, None, [0, 2, 4, 7]),
        (">", 5, None, [5, 8]),
        (">=", 7, None, [5, 8]),
        ("in", 2, 7, [2, 4, 5]),
        ("in", 7, 2, [2, 4, 5]),
        ("==", 300, None, []),
        (">", -1, None, [0, 2, 4, 5, 7, 8]),
    ],
)
def test_find_bus_values(op, operand, operand2, expected):
    assert find_bus_values(VALUES, op, operand, operand2).tolist() == expected


def test_find_bus_values_errors_and_empty():
    with pytest.raises(ValueError):
        find_bus_values(VALUES, "~", 1)
    with pytest.raises(ValueError):
        find_bus_values(VALUES, "in", 1)
    assert len(find_bus_values(VALUES[:0], "==", 1)) == 0


def test_find_bus_values_uint64():
    values = np.array([0, 1 << 63, 1 << 63, 5], dtype=np.uint64)
    assert find_bus_values(values, "==", 1 << 63).tolist() == [1]
    assert find_bus_values(values, ">", 4).tolist() == [1, 3]


def group(name, rows):
    return AnnotationGroup(
        instance=DecoderInstance(decoder_id=name),
        decoder_name=name,
        color_index=0,
        annotations=[
            Annotation(name=row, segments=[AnnotationSegment(0, a, b, values) for a, b, values in segments])
            for row, segments in rows.items()
        ],
    )


GROUPS = [
    group("UART", {"RX": [(30, 40, ["Data: 0x41", "41"]), (10, 20, ["Start"])], "TX": [(5, 8, ["0x42"])]}),
    group("I2C", {"Bits": [(12, 14, ["Address write: 50", "AW: 50"])]}),
]


def test_find_annotations_substring():
    matches = find_annotations(GROUPS, "0x4")
    assert [(m.first_sample, m.text, m.decoder, m.row) for m in matches] == [
        (5, "0x42", "UART", "TX"),
        (30, "Data: 0x41", "UART", "RX"),
    ]
    assert isinstance(matches[0], AnnotationMatch)
    assert matches[1].last_sample == 40


def test_find_annotations_case_and_filters():
    assert [m.first_sample for m in find_annotations(GROUPS, "start")] == [10]
    assert find_annotations(GROUPS, "start", case_sensitive=True) == []
    assert [m.first_sample for m in find_annotations(GROUPS, "", decoder="uart")] == [5, 10, 30]
    assert [m.first_sample for m in find_annotations(GROUPS, "", decoder="UART", row="rx")] == [10, 30]
    # the second value of a segment matches as well
    assert [m.text for m in find_annotations(GROUPS, "aw:")] == ["AW: 50"]


def test_find_annotations_regex():
    assert [m.first_sample for m in find_annotations(GROUPS, r"^0x4[12]$", regex=True)] == [5]
    assert [m.first_sample for m in find_annotations(GROUPS, "0x.", regex=False)] == []
    with pytest.raises(re.error):
        find_annotations(GROUPS, "(", regex=True)


def test_next_match():
    positions = np.array([10, 20, 30])
    assert next_match(positions, 0) == 10
    assert next_match(positions, 10) == 20
    assert next_match(positions, 30) == 10
    assert next_match(positions, 30, wrap=False) is None
    assert next_match(positions, 25, forward=False) == 20
    assert next_match(positions, 20, forward=False) == 10
    assert next_match(positions, 10, forward=False) == 30
    assert next_match(positions, 10, forward=False, wrap=False) is None
    assert next_match(np.zeros(0, dtype=np.int64), 5) is None
    assert match_positions(find_annotations(GROUPS, "")).tolist() == [5, 10, 12, 30]


def test_large_bus_search_is_fast():
    count = 10_000_000
    rng = np.random.default_rng(2)
    values = np.repeat(rng.integers(0, 256, count // 10).astype(np.uint8), 10)
    begin = time.perf_counter()
    found = find_bus_values(values, "in", 0x40, 0x4F)
    position = next_match(found, count // 2)
    elapsed = time.perf_counter() - begin
    assert len(found) and np.all((values[found] >= 0x40) & (values[found] <= 0x4F))
    assert position is not None and position > count // 2
    assert elapsed < 1.0
