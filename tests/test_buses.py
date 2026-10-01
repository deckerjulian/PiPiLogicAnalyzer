"""Bus values, runs, value formatting and symbol tables."""

from __future__ import annotations

import time

import numpy as np
import pytest

from pipilogicanalyzer.core import buses
from pipilogicanalyzer.core.buses import (
    bus_runs,
    bus_values,
    format_value,
    load_symbol_file,
    parse_symbol_table,
    parse_value,
    symbol_table_text,
)
from pipilogicanalyzer.driver.models import (
    AnalyzerChannel,
    BusDefinition,
    BusFormat,
    CaptureSession,
)


def session_of(*channels: list[int], numbers=None) -> CaptureSession:
    numbers = numbers or list(range(len(channels)))
    return CaptureSession(
        frequency=1000,
        capture_channels=[
            AnalyzerChannel(channel_number=n, samples=np.array(c, dtype=np.uint8)) for n, c in zip(numbers, channels)
        ],
    )


def test_bus_values_lsb_first():
    session = session_of([1, 0, 1, 1], [0, 1, 1, 0], [0, 0, 0, 1])
    values = bus_values(session, BusDefinition(channels=[0, 1, 2]))
    assert values.dtype == np.uint8
    assert values.tolist() == [1, 2, 3, 5]


def test_bus_values_order_and_missing_channels():
    session = session_of([1, 0], [0, 1], numbers=[3, 7])
    # channel 5 is missing and reads 0
    values = bus_values(session, BusDefinition(channels=[7, 5, 3]))
    assert values.tolist() == [0b100, 0b001]


@pytest.mark.parametrize("width,dtype", [(1, np.uint8), (8, np.uint8), (9, np.uint16), (16, np.uint16),
                                         (17, np.uint32), (33, np.uint64), (64, np.uint64)])
def test_bus_values_dtype(width, dtype):
    session = session_of([1, 1])
    values = bus_values(session, BusDefinition(channels=[0] * width))
    assert values.dtype == dtype
    assert int(values[0]) == (1 << width) - 1


def test_bus_values_empty_session():
    assert len(bus_values(CaptureSession(), BusDefinition(channels=[0, 1]))) == 0


def test_bus_values_blocks(monkeypatch):
    monkeypatch.setattr(buses, "PACK_BLOCK", 3)
    session = session_of([1, 0, 1, 0, 1, 0, 1], [0, 0, 1, 1, 0, 0, 1])
    assert bus_values(session, BusDefinition(channels=[0, 1])).tolist() == [1, 0, 3, 2, 1, 0, 3]


def test_bus_runs():
    values = np.array([1, 1, 2, 2, 2, 3, 1, 1], dtype=np.uint8)
    starts, runs = bus_runs(values, 0, 8)
    assert starts.tolist() == [0, 2, 5, 6]
    assert runs.tolist() == [1, 2, 3, 1]
    starts, runs = bus_runs(values, 3, 6)
    assert starts.tolist() == [3, 5]
    assert runs.tolist() == [2, 3]
    starts, runs = bus_runs(values, -5, 100)
    assert starts.tolist() == [0, 2, 5, 6]
    assert len(bus_runs(values, 5, 5)[0]) == 0


def test_format_value():
    bus = BusDefinition(channels=list(range(12)))
    assert format_value(0x1F, bus) == "0x01F"
    bus.format = BusFormat.DECIMAL
    assert format_value(0x1F, bus) == "31"
    bus.format = BusFormat.BINARY
    assert format_value(5, BusDefinition(channels=[0, 1, 2, 3], format=BusFormat.BINARY)) == "0b0101"
    signed = BusDefinition(channels=list(range(8)), format=BusFormat.SIGNED)
    assert format_value(0xFF, signed) == "-1"
    assert format_value(0x80, signed) == "-128"
    assert format_value(0x7F, signed) == "127"
    ascii_bus = BusDefinition(channels=list(range(8)), format=BusFormat.ASCII)
    assert format_value(0x41, ascii_bus) == "'A'"
    assert format_value(0x0A, ascii_bus) == "\\x0A"
    assert format_value(0x1, BusDefinition(channels=[0])) == "0x1"


def test_format_value_symbols():
    bus = BusDefinition(channels=list(range(16)), symbols={0xD020: "BORDER"})
    assert format_value(0xD020, bus) == "BORDER"
    assert format_value(np.uint16(0xD020), bus) == "BORDER"
    assert format_value(0xD021, bus) == "0xD021"


def test_parse_value():
    bus = BusDefinition(channels=list(range(16)), symbols={0xD020: "BORDER"})
    assert parse_value("0x1f", bus) == 31
    assert parse_value("$D021", bus) == 0xD021
    assert parse_value("0b101", bus) == 5
    assert parse_value(" 42 ", bus) == 42
    assert parse_value("'A'", bus) == 65
    assert parse_value('"\\n"', bus) == 10
    assert parse_value("border", bus) == 0xD020
    for bad in ("", "xyz", "-1", "0x10000", "1.5"):
        with pytest.raises(ValueError):
            parse_value(bad, bus)


def test_parse_value_signed():
    bus = BusDefinition(channels=list(range(8)), format=BusFormat.SIGNED)
    assert parse_value("-1", bus) == 0xFF
    assert parse_value("-128", bus) == 0x80
    with pytest.raises(ValueError):
        parse_value("-129", bus)


def test_format_parse_round_trip():
    for fmt in BusFormat:
        bus = BusDefinition(channels=list(range(8)), format=fmt)
        for value in (0, 1, 0x41, 0x7F, 0x80, 0xFF):
            assert parse_value(format_value(value, bus), bus) == value, (fmt, value)


def test_parse_symbol_table():
    text = """
# comment
; another
// and another
BORDER = 0xD020
BACKGROUND = 53281   # trailing comment
0xD011 CONTROL1
$D016 CONTROL2
0x10,CSV_A
CSV_B,0x11
"QUOTED", 0x12
SPACE NAME = 0x13
"""
    symbols = parse_symbol_table(text)
    assert symbols == {
        0xD020: "BORDER",
        53281: "BACKGROUND",
        0xD011: "CONTROL1",
        0xD016: "CONTROL2",
        0x10: "CSV_A",
        0x11: "CSV_B",
        0x12: "QUOTED",
        0x13: "SPACE NAME",
    }


def test_parse_symbol_table_csv_header():
    assert parse_symbol_table("address,name\n0x1,ONE\n") == {1: "ONE"}


def test_parse_symbol_table_errors():
    with pytest.raises(ValueError, match="Line 3"):
        parse_symbol_table("A = 1\n\nnonsense\n")
    with pytest.raises(ValueError, match="Line 1"):
        parse_symbol_table("JUSTANAME\n")
    with pytest.raises(ValueError, match="Line 2"):
        parse_symbol_table("A = 1\nB = C\n")


def test_symbol_table_round_trip(tmp_path):
    symbols = {0xD020: "BORDER", 1: "ONE", 0x123: "ODD"}
    text = symbol_table_text(symbols)
    assert text.splitlines()[0] == "ONE = 0x0001"
    assert parse_symbol_table(text) == symbols
    path = tmp_path / "symbols.txt"
    path.write_text(text, encoding="utf-8")
    assert load_symbol_file(path) == symbols
    assert symbol_table_text({}) == ""


def test_large_capture_is_fast():
    count = 10_000_000
    rng = np.random.default_rng(1)
    channels = [np.repeat(rng.integers(0, 2, count // 100, dtype=np.uint8), 100) for _ in range(8)]
    session = CaptureSession(capture_channels=[AnalyzerChannel(channel_number=n, samples=s) for n, s in enumerate(channels)])
    begin = time.perf_counter()
    values = bus_values(session, BusDefinition(channels=list(range(8))))
    starts, _ = bus_runs(values, 0, count)
    elapsed = time.perf_counter() - begin
    assert len(values) == count
    assert int(values[150]) == sum(int(channels[n][150]) << n for n in range(8))
    assert np.all(starts % 100 == 0)
    assert elapsed < 2.0
