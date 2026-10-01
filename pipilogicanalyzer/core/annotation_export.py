# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Export of decoder annotations to CSV and JSON.

Times are given in seconds relative to the trigger (negative before it), like the time column of
the CSV export of the samples; sample numbers count from the first sample of the capture.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional, Sequence

from ..driver.models import CaptureSession
from ..sigrok.provider import AnnotationGroup

CSV_HEADER = (
    "Start time (s)",
    "End time (s)",
    "Start sample",
    "End sample",
    "Decoder",
    "Row",
    "Value",
)


@dataclass
class AnnotationRecord:
    """One annotation of a decoder, e.g. a byte of a UART or the address of an I²C transfer."""

    start_sample: int
    end_sample: int
    #: Seconds relative to the trigger
    start_time: float
    end_time: float
    #: Name of the decoder (or the label of its instance)
    decoder: str
    #: Annotation row of the decoder (``"Bits"``, ``"Data"``, ...)
    row: str
    #: Longest text of the annotation
    value: str
    #: Every text of the annotation, longest first (decoders add shorter forms for narrow views)
    values: list[str] = field(default_factory=list)
    decoder_id: str = ""


def annotation_records(
    groups: Iterable[AnnotationGroup], session: CaptureSession
) -> list[AnnotationRecord]:
    """The annotations of ``groups`` as flat records, sorted by their start."""
    frequency = float(max(int(session.frequency), 1))
    trigger = int(session.pre_trigger_samples)
    records: list[tuple[int, int, int, AnnotationRecord]] = []
    for group_index, group in enumerate(groups):
        for row_index, annotation in enumerate(group.annotations):
            for segment in annotation.segments:
                values = [str(value) for value in segment.values]
                records.append(
                    (
                        segment.first_sample,
                        group_index,
                        row_index,
                        AnnotationRecord(
                            start_sample=int(segment.first_sample),
                            end_sample=int(segment.last_sample),
                            start_time=(segment.first_sample - trigger) / frequency,
                            end_time=(segment.last_sample - trigger) / frequency,
                            decoder=group.decoder_name,
                            row=annotation.name,
                            value=values[0] if values else "",
                            values=values,
                            decoder_id=group.instance.decoder_id,
                        ),
                    )
                )
    records.sort(key=lambda item: item[:3])
    return [record for *_, record in records]


def export_annotations_csv(
    path: str, groups: Sequence[AnnotationGroup], session: CaptureSession
) -> int:
    """Write the annotations as CSV (:data:`CSV_HEADER`); returns the number of annotations."""
    records = annotation_records(groups, session)
    _make_directory(path)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for record in records:
            writer.writerow(
                (
                    f"{record.start_time:.12g}",
                    f"{record.end_time:.12g}",
                    record.start_sample,
                    record.end_sample,
                    record.decoder,
                    record.row,
                    record.value,
                )
            )
    return len(records)


def annotations_to_dict(groups: Sequence[AnnotationGroup], session: CaptureSession) -> dict:
    """The JSON document of :func:`export_annotations_json`."""
    records = annotation_records(groups, session)
    errors = [
        {"decoder": group.decoder_name, "decoder_id": group.instance.decoder_id, "error": group.error}
        for group in groups
        if group.error
    ]
    return {
        "samplerate": int(session.frequency),
        "trigger_sample": int(session.pre_trigger_samples),
        "sample_count": int(session.sample_count()),
        "annotations": [asdict(record) for record in records],
        "errors": errors,
    }


def export_annotations_json(
    path: str, groups: Sequence[AnnotationGroup], session: CaptureSession
) -> int:
    """Write the annotations as JSON; returns the number of annotations.

    ``{"samplerate": ..., "trigger_sample": ..., "sample_count": ..., "annotations": [{"start_sample",
    "end_sample", "start_time", "end_time", "decoder", "row", "value", "values", "decoder_id"}, ...],
    "errors": [{"decoder", "decoder_id", "error"}, ...]}``
    """
    document = annotations_to_dict(groups, session)
    _make_directory(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
    return len(document["annotations"])


def export_annotations(
    path: str, groups: Sequence[AnnotationGroup], session: CaptureSession, kind: Optional[str] = None
) -> int:
    """CSV or JSON, chosen by ``kind`` (``"csv"``/``"json"``) or the extension of ``path``."""
    kind = (kind or os.path.splitext(path)[1].lstrip(".")).lower()
    if kind == "json":
        return export_annotations_json(path, groups, session)
    if kind == "csv":
        return export_annotations_csv(path, groups, session)
    raise ValueError(f"Unknown annotation format '{kind}': use .csv or .json.")


def _make_directory(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
