"""Export of decoder annotations."""

from __future__ import annotations

import csv
import json

import numpy as np
import pytest

from openscilab.core.annotation_export import (
    CSV_HEADER,
    annotation_records,
    export_annotations,
    export_annotations_csv,
    export_annotations_json,
)
from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.sigrok.engine import Annotation, AnnotationSegment
from openscilab.sigrok.provider import AnnotationGroup, DecoderInstance, SigrokProvider


def session_with_clock() -> CaptureSession:
    session = CaptureSession(frequency=1000, pre_trigger_samples=10, post_trigger_samples=30)
    clock = ((np.arange(40) // 4) % 2).astype(np.uint8)  # rising edges at 4, 12, 20, 28, 36
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="CLK", samples=clock)]
    return session


def manual_groups() -> list[AnnotationGroup]:
    first = AnnotationGroup(
        instance=DecoderInstance("uart"),
        decoder_name="UART",
        color_index=0,
        annotations=[
            Annotation("Data", [AnnotationSegment(0, 20, 30, ["0x41 'A'", "41"])]),
            Annotation("Bits", [AnnotationSegment(1, 10, 12, ["1"])]),
        ],
    )
    second = AnnotationGroup(
        instance=DecoderInstance("spi"), decoder_name="SPI", color_index=1,
        annotations=[Annotation("MOSI", [AnnotationSegment(0, 10, 15, ["7"])])], error="ValueError: x",
    )
    return [first, second]


def test_records_are_sorted_and_relative_to_the_trigger():
    records = annotation_records(manual_groups(), session_with_clock())
    assert [(r.decoder, r.row, r.start_sample) for r in records] == [
        ("UART", "Bits", 10), ("SPI", "MOSI", 10), ("UART", "Data", 20),
    ]
    data = records[2]
    assert data.start_time == pytest.approx(0.010)
    assert data.end_time == pytest.approx(0.020)
    assert data.value == "0x41 'A'" and data.values == ["0x41 'A'", "41"]
    assert records[0].start_time == 0.0
    assert data.decoder_id == "uart"


def test_csv(tmp_path):
    path = str(tmp_path / "out" / "annotations.csv")
    assert export_annotations_csv(path, manual_groups(), session_with_clock()) == 3
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == CSV_HEADER
    assert rows[3] == ["0.01", "0.02", "20", "30", "UART", "Data", "0x41 'A'"]


def test_json(tmp_path):
    path = str(tmp_path / "annotations.json")
    assert export_annotations(path, manual_groups(), session_with_clock()) == 3
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    assert document["samplerate"] == 1000
    assert document["trigger_sample"] == 10
    assert document["sample_count"] == 40
    assert document["annotations"][2]["values"] == ["0x41 'A'", "41"]
    assert document["errors"] == [{"decoder": "SPI", "decoder_id": "spi", "error": "ValueError: x"}]


def test_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="csv"):
        export_annotations(str(tmp_path / "a.txt"), manual_groups(), session_with_clock())


def test_decoder_output(tmp_path, decoder_registry):
    provider = SigrokProvider(decoder_registry)
    provider.add_instance(DecoderInstance("testdec", channel_map={0: 0}))
    session = session_with_clock()
    groups = provider.run(session)

    path = str(tmp_path / "edges.json")
    assert export_annotations_json(path, groups, session) == 5
    with open(path, encoding="utf-8") as handle:
        annotations = json.load(handle)["annotations"]
    assert [item["start_sample"] for item in annotations] == [4, 12, 20, 28, 36]
    assert annotations[0]["start_time"] == pytest.approx(-0.006)
    assert annotations[0]["value"] == "edge 1"
    assert annotations[0]["decoder"] == "TestDec"
    assert annotations[0]["row"] == "Edges"
