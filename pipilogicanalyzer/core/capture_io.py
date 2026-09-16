# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading and writing captures.

``.lac`` files are fully compatible with the original C# application (including
files written by version 5 and earlier, which stored the samples as a single
packed ``UInt128`` array).  ``.lac.gz`` is accepted and written transparently,
which typically shrinks a capture file by more than an order of magnitude.

Two additional export formats are provided: CSV (same layout as the original
export) and VCD, which can be opened with PulseView, GTKWave or any other
waveform viewer.
"""

from __future__ import annotations

import gzip
import io
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence, TextIO

import numpy as np

from ..driver.models import AnalyzerChannel, BurstInfo, CaptureSession, TriggerType
from .regions import SampleRegion


@dataclass
class ExportedCapture:
    session: CaptureSession
    regions: list[SampleRegion] = field(default_factory=list)


# --------------------------------------------------------------------------- io
def _open_text(path: str, mode: str) -> TextIO:
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, mode.replace("t", "") + "b"), encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def load_capture(path: str) -> ExportedCapture:
    """Load a ``.lac`` (or ``.lac.gz``) capture file."""
    with _open_text(path, "rt") as handle:
        data = json.load(handle)
    return capture_from_dict(data)


def save_capture(path: str, session: CaptureSession, regions: Sequence[SampleRegion] = ()) -> None:
    """Write a ``.lac`` capture file readable by the original application."""
    payload = capture_to_dict(session, regions)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with _open_text(path, "wt") as handle:
        json.dump(payload, handle)


# ----------------------------------------------------------------- (de)serialise
def capture_to_dict(session: CaptureSession, regions: Sequence[SampleRegion] = ()) -> dict:
    return {
        "Settings": session_to_dict(session),
        "Samples": None,
        "SelectedRegions": [region.to_dict() for region in regions],
    }


def capture_from_dict(data: dict) -> ExportedCapture:
    settings = data.get("Settings")
    if not isinstance(settings, dict):
        raise ValueError("The file does not contain a capture (missing 'Settings').")

    session = session_from_dict(settings)

    legacy_samples = data.get("Samples")
    if legacy_samples:
        _extract_legacy_samples(session, legacy_samples)

    regions = [SampleRegion.from_dict(item) for item in (data.get("SelectedRegions") or [])]
    return ExportedCapture(session=session, regions=regions)


def session_to_dict(session: CaptureSession, include_samples: bool = True) -> dict:
    return {
        "Frequency": int(session.frequency),
        "PreTriggerSamples": int(session.pre_trigger_samples),
        "PostTriggerSamples": int(session.post_trigger_samples),
        "TotalSamples": int(session.total_samples),
        "LoopCount": int(session.loop_count),
        "MeasureBursts": bool(session.measure_bursts),
        "CaptureChannels": [channel_to_dict(c, include_samples) for c in session.capture_channels],
        "Bursts": None
        if not session.bursts
        else [
            {
                "BurstSampleStart": burst.burst_sample_start,
                "BurstSampleEnd": burst.burst_sample_end,
                "BurstSampleGap": burst.burst_sample_gap,
                "BurstTimeGap": burst.burst_time_gap,
            }
            for burst in session.bursts
        ],
        "TriggerType": int(session.trigger_type),
        "TriggerChannel": int(session.trigger_channel),
        "TriggerInverted": bool(session.trigger_inverted),
        "TriggerBitCount": int(session.trigger_bit_count),
        "TriggerPattern": int(session.trigger_pattern),
    }


def session_from_dict(data: dict) -> CaptureSession:
    session = CaptureSession(
        frequency=int(data.get("Frequency", 1)),
        pre_trigger_samples=int(data.get("PreTriggerSamples", 0)),
        post_trigger_samples=int(data.get("PostTriggerSamples", 0)),
        loop_count=int(data.get("LoopCount", 0)),
        measure_bursts=bool(data.get("MeasureBursts", False)),
        trigger_type=TriggerType(int(data.get("TriggerType", 0))),
        trigger_channel=int(data.get("TriggerChannel", 0)),
        trigger_inverted=bool(data.get("TriggerInverted", False)),
        trigger_bit_count=int(data.get("TriggerBitCount", 0)),
        trigger_pattern=int(data.get("TriggerPattern", 0)),
    )
    session.capture_channels = [
        channel_from_dict(item) for item in (data.get("CaptureChannels") or [])
    ]
    bursts = data.get("Bursts")
    if bursts:
        session.bursts = [
            BurstInfo(
                burst_sample_start=int(item.get("BurstSampleStart", 0)),
                burst_sample_end=int(item.get("BurstSampleEnd", 0)),
                burst_sample_gap=int(item.get("BurstSampleGap", 0)),
                burst_time_gap=int(item.get("BurstTimeGap", 0)),
            )
            for item in bursts
        ]
    return session


def channel_to_dict(channel: AnalyzerChannel, include_samples: bool = True) -> dict:
    data: dict[str, Any] = {
        "TextualChannelNumber": channel.textual_channel_number,
        "ChannelNumber": int(channel.channel_number),
        "ChannelName": channel.channel_name or "",
        "ChannelColor": None if channel.channel_color is None else int(channel.channel_color),
        "Hidden": bool(channel.hidden),
        "Samples": None
        if (channel.samples is None or not include_samples)
        else channel.samples.astype(np.uint8).tolist(),
    }
    return data


def channel_from_dict(data: dict) -> AnalyzerChannel:
    samples = data.get("Samples")
    channel = AnalyzerChannel(
        channel_number=int(data.get("ChannelNumber", 0)),
        channel_name=data.get("ChannelName") or "",
        channel_color=None if data.get("ChannelColor") in (None, "") else int(data["ChannelColor"]),
        hidden=bool(data.get("Hidden", False)),
        samples=None if samples is None else np.asarray(samples, dtype=np.uint8),
    )
    return channel


def _extract_legacy_samples(session: CaptureSession, packed: Iterable[Any]) -> None:
    """Unpack the ``UInt128`` sample array used by older ``.lac`` files."""
    values = [int(value) for value in packed]
    for index, channel in enumerate(session.capture_channels):
        mask = 1 << index
        channel.samples = np.fromiter(
            (1 if value & mask else 0 for value in values), dtype=np.uint8, count=len(values)
        )


# ------------------------------------------------------------------- exporters
def export_csv(path: str, session: CaptureSession, include_time: bool = False) -> None:
    """Export the capture as comma separated values."""
    channels = [c for c in session.capture_channels if c.samples is not None]
    if not channels:
        raise ValueError("The capture contains no samples to export.")

    sample_count = min(int(c.samples.shape[0]) for c in channels)
    matrix = np.vstack([c.samples[:sample_count] for c in channels]).T

    header = [c.display_name for c in channels]
    if include_time:
        header.insert(0, "Time")
        times = (np.arange(sample_count) - session.pre_trigger_samples) / float(session.frequency)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        if include_time:
            for index in range(sample_count):
                handle.write(
                    f"{times[index]:.12g},"
                    + ",".join(str(int(value)) for value in matrix[index])
                    + "\n"
                )
        else:
            np.savetxt(handle, matrix, fmt="%d", delimiter=",")


_VCD_IDENTIFIERS = "!#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"


def export_vcd(path: str, session: CaptureSession) -> None:
    """Export the capture as a VCD file (PulseView, GTKWave, ...)."""
    channels = [c for c in session.capture_channels if c.samples is not None]
    if not channels:
        raise ValueError("The capture contains no samples to export.")

    sample_count = min(int(c.samples.shape[0]) for c in channels)
    frequency = max(int(session.frequency), 1)

    # Pick a timescale that keeps the sample period an integer number of units.
    period_ns = 1_000_000_000.0 / frequency
    if period_ns >= 1:
        timescale, unit_ns = "1 ns", 1.0
    elif period_ns >= 0.001:
        timescale, unit_ns = "1 ps", 0.001
    else:
        timescale, unit_ns = "1 fs", 0.000001
    ticks_per_sample = max(int(round(period_ns / unit_ns)), 1)

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("$date generated by PiPiLogicAnalyzer $end\n")
        handle.write("$version PiPiLogicAnalyzer Python GUI $end\n")
        handle.write(f"$timescale {timescale} $end\n")
        handle.write("$scope module logic $end\n")

        identifiers = []
        for index, channel in enumerate(channels):
            identifier = _VCD_IDENTIFIERS[index % len(_VCD_IDENTIFIERS)]
            if index >= len(_VCD_IDENTIFIERS):
                identifier += _VCD_IDENTIFIERS[index // len(_VCD_IDENTIFIERS)]
            identifiers.append(identifier)
            name = (channel.display_name or channel.textual_channel_number).replace(" ", "_")
            handle.write(f"$var wire 1 {identifier} {name} $end\n")

        handle.write("$upscope $end\n")
        handle.write("$enddefinitions $end\n")

        handle.write("#0\n")
        handle.write("$dumpvars\n")
        for identifier, channel in zip(identifiers, channels):
            handle.write(f"{int(channel.samples[0])}{identifier}\n")
        handle.write("$end\n")

        # Only transitions are written, which keeps the file small.
        events: list[tuple[int, str]] = []
        for identifier, channel in zip(identifiers, channels):
            samples = channel.samples[:sample_count]
            changes = np.flatnonzero(samples[1:] != samples[:-1]) + 1
            for position in changes:
                events.append((int(position), f"{int(samples[position])}{identifier}"))

        events.sort(key=lambda item: item[0])
        last_position: Optional[int] = None
        for position, value in events:
            if position != last_position:
                handle.write(f"#{position * ticks_per_sample}\n")
                last_position = position
            handle.write(value + "\n")

        handle.write(f"#{sample_count * ticks_per_sample}\n")
