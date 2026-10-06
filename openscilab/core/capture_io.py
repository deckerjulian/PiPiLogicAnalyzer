# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
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

import base64
import binascii
import csv
import gzip
import io
import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence, TextIO

import numpy as np

from ..driver.models import (
    AnalogChannel,
    AnalyzerChannel,
    BurstInfo,
    BusDefinition,
    BusFormat,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)
from .files import atomic_open
from .regions import SampleRegion
from .sample_store import DiskAllocator, MemoryAllocator

#: gzip level of ``.lac.gz`` (9, the default of gzip.open, is much slower for a few percent)
GZIP_LEVEL = 6

# Samples in a .lac file. The original software writes them as a JSON list with a number per
# sample (``[0,1,1,0,...]``): 3 bytes and a Python object per sample, which makes a capture of
# 5 million samples on 24 channels a file of 360 MB that takes a gigabyte of memory to read.
# openSciLab packs them: the bits of a channel (8 samples per byte), compressed, as text.
# Both forms are read; ``compatible=True`` writes the lists for the original software.
#: samples packed and compressed in one piece (a multiple of 8; a capture on disk is read in
#: pieces of this size, never as a whole)
PACK_BLOCK = 8 << 20
#: sample bytes (channels x samples) above which a capture is loaded into files on disk
#: instead of memory (the limit of captures kept in memory, driver.base.MAX_SAMPLE_BYTES)
DISK_LOAD_BYTES = 1 << 30


def _pack(blocks: Iterable[bytes]) -> str:
    compressor = zlib.compressobj(6)
    parts = [compressor.compress(block) for block in blocks]
    parts.append(compressor.flush())
    return base64.b64encode(b"".join(parts)).decode("ascii")


def _unpacked(text: str, block_bytes: int) -> Iterable[bytes]:
    """The bytes packed in ``text``, in pieces of at most ``block_bytes``."""
    try:
        data = base64.b64decode(text, validate=True)
        decompressor = zlib.decompressobj()
        pending = data
        while True:
            piece = decompressor.decompress(pending, block_bytes)
            pending = decompressor.unconsumed_tail
            if piece:
                yield piece
            elif decompressor.eof or not pending:
                return
    except (binascii.Error, zlib.error, ValueError) as error:
        raise ValueError(f"The samples in the file are damaged ({error}).") from None


def _is_binary(samples: np.ndarray) -> bool:
    return all(not (np.asarray(samples[start : start + PACK_BLOCK]) > 1).any()
               for start in range(0, len(samples), PACK_BLOCK))


def pack_bits(samples: np.ndarray) -> str:
    """The 0/1 ``samples`` of a channel as text: 8 samples per byte, compressed."""
    return _pack(np.packbits(np.asarray(samples[start : start + PACK_BLOCK], dtype=np.uint8)).tobytes()
                 for start in range(0, len(samples), PACK_BLOCK))


def unpack_bits(text: str, count: int, allocator=None) -> np.ndarray:
    """``count`` samples packed by :func:`pack_bits` (raises ``ValueError`` for damaged text)."""
    result = (allocator or MemoryAllocator()).zeros(count, np.uint8)
    position = 0
    for piece in _unpacked(text, PACK_BLOCK // 8):
        bits = np.unpackbits(np.frombuffer(piece, dtype=np.uint8))
        take = min(len(bits), count - position)
        result[position : position + take] = bits[:take]
        position += take
    if position < count:
        raise ValueError(f"The samples in the file are damaged ({position} of {count} are there).")
    return result


def pack_values(values: np.ndarray, dtype: str) -> str:
    """Numbers (the raw values of an analog channel, times) as text: their bytes, compressed."""
    step = PACK_BLOCK
    return _pack(np.ascontiguousarray(values[start : start + step], dtype=dtype).tobytes()
                 for start in range(0, len(values), step))


def unpack_values(text: str, count: int, dtype: str, allocator=None) -> np.ndarray:
    kind = np.dtype(dtype)
    result = (allocator or MemoryAllocator()).zeros(count, kind.newbyteorder("="))
    position, rest = 0, b""
    for piece in _unpacked(text, PACK_BLOCK):
        piece = rest + piece
        whole = len(piece) // kind.itemsize * kind.itemsize
        values = np.frombuffer(piece[:whole], dtype=kind)
        rest = piece[whole:]
        take = min(len(values), count - position)
        result[position : position + take] = values[:take]
        position += take
    if position < count:
        raise ValueError(f"The samples in the file are damaged ({position} of {count} are there).")
    return result


@dataclass
class ExportedCapture:
    session: CaptureSession
    regions: list[SampleRegion] = field(default_factory=list)
    #: markers: (sample, name)
    bookmarks: list[tuple[int, str]] = field(default_factory=list)
    #: numbers of the channels pinned above the others
    pinned: list[int] = field(default_factory=list)
    #: the file holds its samples as the original software writes them (a number per sample):
    #: saving it again keeps that form, so that software can still open it
    compatible: bool = False


# --------------------------------------------------------------------------- io
def _open_text(path: str, mode: str) -> TextIO:
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, mode.replace("t", "") + "b"), encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def load_capture(path: str) -> ExportedCapture:
    """Load a ``.lac`` (or ``.lac.gz``) capture file; raises ``OSError`` when it cannot be read
    and ``ValueError`` when it is no capture or damaged."""
    try:
        with _open_text(path, "rt") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("The file does not contain a capture.")
        return capture_from_dict(data)
    except (EOFError, zlib.error, UnicodeDecodeError) as error:  # a file cut off or overwritten
        raise ValueError(f"The file is damaged ({error}).") from None
    except (TypeError, AttributeError, KeyError) as error:  # JSON, but not that of a capture
        raise ValueError(f"The file does not contain a capture ({type(error).__name__}: {error}).") from None


def save_capture(path: str, session: CaptureSession, regions: Sequence[SampleRegion] = (),
                 bookmarks: Sequence[tuple[int, str]] = (), pinned: Sequence[int] = (),
                 compatible: bool = False) -> None:
    """Write a ``.lac`` capture file (``.lac.gz``: compressed).

    The samples are packed (small files, quick to write and read, also for captures on disk).
    ``compatible=True`` writes them as the original LogicAnalyzer software does, a number per
    sample, for a file that software can open.
    """
    payload = capture_to_dict(session, regions, bookmarks, pinned, compatible)
    if path.endswith(".gz"):
        with atomic_open(path, "wb") as target, \
                gzip.GzipFile(fileobj=target, mode="wb", compresslevel=GZIP_LEVEL) as packed, \
                io.TextIOWrapper(packed, encoding="utf-8") as handle:
            json.dump(payload, handle)
    else:
        with atomic_open(path) as handle:
            json.dump(payload, handle)


# ----------------------------------------------------------------- (de)serialise
def capture_to_dict(session: CaptureSession, regions: Sequence[SampleRegion] = (),
                    bookmarks: Sequence[tuple[int, str]] = (), pinned: Sequence[int] = (),
                    compatible: bool = False) -> dict:
    data = {
        "Settings": session_to_dict(session, compatible=compatible),
        "Samples": None,
        "SelectedRegions": [region.to_dict() for region in regions],
    }
    # openSciLab: markers and pinned channels (the original software ignores unknown properties)
    if bookmarks:
        data["Bookmarks"] = [{"Sample": int(sample), "Name": str(name)} for sample, name in bookmarks]
    if pinned:
        data["PinnedChannels"] = [int(number) for number in pinned]
    return data


def capture_from_dict(data: dict) -> ExportedCapture:
    settings = data.get("Settings")
    if not isinstance(settings, dict):
        raise ValueError("The file does not contain a capture (missing 'Settings').")

    session = session_from_dict(settings)

    legacy_samples = data.get("Samples")
    if legacy_samples:
        _extract_legacy_samples(session, legacy_samples)

    regions = [SampleRegion.from_dict(item) for item in (data.get("SelectedRegions") or [])]
    bookmarks = [(int(item.get("Sample", 0)), str(item.get("Name") or ""))
                 for item in (data.get("Bookmarks") or []) if isinstance(item, dict)]
    pinned = [int(number) for number in (data.get("PinnedChannels") or [])]
    stored = [item for key in ("CaptureChannels", "AnalogChannels") for item in (settings.get(key) or [])
              if isinstance(item, dict)]
    lists = bool(legacy_samples) or any(item.get("Samples") is not None for item in stored)
    packed = any(item.get("SamplesPacked") is not None for item in stored)
    return ExportedCapture(session=session, regions=regions, bookmarks=bookmarks, pinned=pinned,
                           compatible=lists and not packed)


def session_to_dict(session: CaptureSession, include_samples: bool = True, compatible: bool = False) -> dict:
    """``compatible``: the samples as lists of numbers (the original software), not packed."""
    state_times = None if session.state_times is None or not include_samples else session.state_times
    data = {
        "Frequency": int(session.frequency),
        "PreTriggerSamples": int(session.pre_trigger_samples),
        "PostTriggerSamples": int(session.post_trigger_samples),
        "TotalSamples": int(session.total_samples),
        "LoopCount": int(session.loop_count),
        "MeasureBursts": bool(session.measure_bursts),
        "CaptureChannels": [channel_to_dict(c, include_samples, compatible) for c in session.capture_channels],
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
        "AcquisitionMode": session.acquisition_mode,
        "ThresholdVoltage": session.threshold_voltage,
        "Continuous": session.continuous,
        "ToDisk": session.to_disk,
        # Extensions of this application (like the four keys above): the original software
        # ignores unknown properties, and files without them load with the defaults.
        "TriggerSequence": sequence_to_dict(session.trigger_sequence),
        "SoftwareTrigger": bool(session.software_trigger),
        "ClockChannel": None if session.clock_channel is None else int(session.clock_channel),
        "ClockEdge": session.clock_edge.value,
        "Buses": [bus_to_dict(bus) for bus in session.buses],
        # openSciLab: analog channels and the times of the states of a state capture.
        "AnalogChannels": [analog_to_dict(channel, include_samples, compatible)
                           for channel in session.analog_channels],
        "StateTimes": None if state_times is None or not compatible
        else [float(value) for value in np.asarray(state_times, dtype=np.float64)],
    }
    if state_times is not None and not compatible:
        data["StateTimeCount"] = len(state_times)
        data["StateTimesPacked"] = pack_values(np.asarray(state_times, dtype=np.float64), "<f8")
    return data


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
        acquisition_mode=str(data.get("AcquisitionMode") or "buffer"),
        threshold_voltage=(
            float(data["ThresholdVoltage"]) if data.get("ThresholdVoltage") is not None else None
        ),
        continuous=bool(data.get("Continuous", False)),
        to_disk=bool(data.get("ToDisk", False)),
        trigger_sequence=sequence_from_dict(data.get("TriggerSequence")),
        software_trigger=bool(data.get("SoftwareTrigger", False)),
        clock_channel=(
            int(data["ClockChannel"]) if data.get("ClockChannel") is not None else None
        ),
        clock_edge=_enum(EdgeKind, data.get("ClockEdge"), EdgeKind.RISING),
        buses=[
            bus_from_dict(item) for item in (data.get("Buses") or []) if isinstance(item, dict)
        ],
    )
    channels = data.get("CaptureChannels") or []
    # a capture too large for memory is unpacked into files on disk (as it was recorded)
    packed_bytes = sum(int(item.get("SampleCount") or 0) for item in channels if isinstance(item, dict))
    allocator = DiskAllocator() if packed_bytes > DISK_LOAD_BYTES else MemoryAllocator()
    session.capture_channels = [channel_from_dict(item, allocator) for item in channels]
    session.analog_channels = [
        analog_from_dict(item, allocator) for item in (data.get("AnalogChannels") or []) if isinstance(item, dict)
    ]
    state_times = data.get("StateTimes")
    if data.get("StateTimesPacked"):
        session.state_times = unpack_values(data["StateTimesPacked"], int(data.get("StateTimeCount") or 0), "<f8")
    elif state_times:
        session.state_times = np.asarray(state_times, dtype=np.float64)
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


def _enum(kind, value: Any, default):
    """``kind(value)``, or ``default`` for a missing or unknown value."""
    try:
        return kind(value)
    except ValueError:
        return default


def _optional_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def sequence_to_dict(sequence: Optional[TriggerSequence]) -> Optional[dict]:
    """Trigger sequence as stored in the ``TriggerSequence`` key (``None``: no sequence)."""
    if sequence is None:
        return None
    return {
        "Stages": [
            {
                "Condition": {
                    "Kind": stage.condition.kind.value,
                    "Channel": int(stage.condition.channel),
                    "Edge": stage.condition.edge.value,
                    "Mask": int(stage.condition.mask),
                    "Value": int(stage.condition.value),
                    "MinNs": _optional_int(stage.condition.min_ns),
                    "MaxNs": _optional_int(stage.condition.max_ns),
                },
                "Count": int(stage.count),
                "WithinNs": _optional_int(stage.within_ns),
            }
            for stage in sequence.stages
        ]
    }


def sequence_from_dict(data: Any) -> Optional[TriggerSequence]:
    if not isinstance(data, dict):
        return None
    stages = []
    for item in data.get("Stages") or []:
        if not isinstance(item, dict):
            continue
        condition = item.get("Condition") or {}
        stages.append(
            TriggerStage(
                condition=TriggerCondition(
                    kind=_enum(ConditionKind, condition.get("Kind"), ConditionKind.EDGE),
                    channel=int(condition.get("Channel", 0)),
                    edge=_enum(EdgeKind, condition.get("Edge"), EdgeKind.RISING),
                    mask=int(condition.get("Mask", 0)),
                    value=int(condition.get("Value", 0)),
                    min_ns=_optional_int(condition.get("MinNs")),
                    max_ns=_optional_int(condition.get("MaxNs")),
                ),
                count=int(item.get("Count", 1)),
                within_ns=_optional_int(item.get("WithinNs")),
            )
        )
    return TriggerSequence(stages=stages)


def bus_to_dict(bus: BusDefinition) -> dict:
    """A bus as stored in the ``Buses`` key; the symbol table has string keys (JSON objects)."""
    return {
        "Name": bus.name,
        "Channels": [int(channel) for channel in bus.channels],
        "Format": bus.format.value,
        "Symbols": {str(int(value)): str(name) for value, name in bus.symbols.items()},
        "Color": _optional_int(bus.color),
    }


def bus_from_dict(data: dict) -> BusDefinition:
    symbols: dict[int, str] = {}
    for key, name in (data.get("Symbols") or {}).items():
        try:
            value = int(key)
        except ValueError:
            try:
                value = int(str(key), 0)  # "0xD020", written by hand
            except ValueError:
                continue
        symbols[value] = str(name)
    return BusDefinition(
        name=str(data.get("Name") or "Bus"),
        channels=[int(channel) for channel in (data.get("Channels") or [])],
        format=_enum(BusFormat, data.get("Format"), BusFormat.HEX),
        symbols=symbols,
        color=_optional_int(data.get("Color")),
    )


def channel_to_dict(channel: AnalyzerChannel, include_samples: bool = True, compatible: bool = False) -> dict:
    samples = channel.samples if include_samples else None
    # packed unless the original software is to read the file, or a sample is neither 0 nor 1
    packed = samples is not None and not compatible and _is_binary(samples)
    data: dict[str, Any] = {
        "TextualChannelNumber": channel.textual_channel_number,
        "ChannelNumber": int(channel.channel_number),
        "ChannelName": channel.channel_name or "",
        "ChannelColor": None if channel.channel_color is None else int(channel.channel_color),
        "Hidden": bool(channel.hidden),
        "Samples": None if samples is None or packed else np.asarray(samples, dtype=np.uint8).tolist(),
    }
    if packed:
        data["SampleCount"] = int(len(samples))
        data["SamplesPacked"] = pack_bits(samples)
    return data


def channel_from_dict(data: dict, allocator=None) -> AnalyzerChannel:
    samples = data.get("Samples")
    if data.get("SamplesPacked") is not None:
        samples = unpack_bits(str(data["SamplesPacked"]), int(data.get("SampleCount") or 0), allocator)
    channel = AnalyzerChannel(
        channel_number=int(data.get("ChannelNumber", 0)),
        channel_name=data.get("ChannelName") or "",
        channel_color=None if data.get("ChannelColor") in (None, "") else int(data["ChannelColor"]),
        hidden=bool(data.get("Hidden", False)),
        samples=None if samples is None else np.asarray(samples, dtype=np.uint8),
    )
    return channel


def analog_to_dict(channel: AnalogChannel, include_samples: bool = True, compatible: bool = False) -> dict:
    raw = channel.raw if include_samples else None
    data = {
        "ChannelNumber": int(channel.channel_number),
        "ChannelName": channel.channel_name or "",
        "Unit": channel.unit,
        "Scale": float(channel.scale),
        "Offset": float(channel.offset),
        "Rate": None if channel.rate is None else int(channel.rate),
        "ChannelColor": None if channel.channel_color is None else int(channel.channel_color),
        "Hidden": bool(channel.hidden),
        "Samples": None if raw is None or not compatible else np.asarray(raw, dtype=np.int16).tolist(),
    }
    if raw is not None and not compatible:
        data["SampleCount"] = int(len(raw))
        data["SamplesPacked"] = pack_values(raw, "<i2")
    return data


def analog_from_dict(data: dict, allocator=None) -> AnalogChannel:
    samples = data.get("Samples")
    if data.get("SamplesPacked") is not None:
        samples = unpack_values(str(data["SamplesPacked"]), int(data.get("SampleCount") or 0), "<i2", allocator)
    return AnalogChannel(
        channel_number=int(data.get("ChannelNumber", 0)),
        channel_name=data.get("ChannelName") or "",
        unit=str(data.get("Unit") or "V"),
        scale=float(data.get("Scale", 1.0)),
        offset=float(data.get("Offset", 0.0)),
        rate=None if data.get("Rate") in (None, "") else int(data["Rate"]),
        channel_color=None if data.get("ChannelColor") in (None, "") else int(data["ChannelColor"]),
        hidden=bool(data.get("Hidden", False)),
        raw=None if samples is None else np.asarray(samples, dtype=np.int16),
    )


def _analog_on_session_rate(session: CaptureSession, channel: AnalogChannel, count: int) -> np.ndarray:
    """The values of ``channel`` at the ``count`` samples of the session (its own rate resampled)."""
    values = channel.volts()
    if not channel.rate or channel.rate == session.frequency or not len(values):
        return values[:count] if len(values) >= count else np.concatenate([values, np.full(count - len(values), np.nan)])
    index = np.minimum((np.arange(count) * channel.rate / session.frequency).astype(np.int64), len(values) - 1)
    return values[index]


def _extract_legacy_samples(session: CaptureSession, packed: Iterable[Any]) -> None:
    """Unpack the ``UInt128`` sample array used by older ``.lac`` files."""
    values = [int(value) for value in packed]
    # the 128 bits of every sample as two arrays of 64, shifted and masked per channel
    low = np.array([value & 0xFFFFFFFFFFFFFFFF for value in values], dtype=np.uint64)
    high = np.array([(value >> 64) & 0xFFFFFFFFFFFFFFFF for value in values], dtype=np.uint64)
    for index, channel in enumerate(session.capture_channels):
        if index < 64:
            channel.samples = ((low >> np.uint64(index)) & np.uint64(1)).astype(np.uint8)
        elif index < 128:
            channel.samples = ((high >> np.uint64(index - 64)) & np.uint64(1)).astype(np.uint8)
        else:
            channel.samples = np.zeros(len(values), dtype=np.uint8)


# ------------------------------------------------------------------- exporters
#: samples written at once by the exporters (a capture on disk is never held in memory as text)
EXPORT_BLOCK = 1 << 18
#: the numbers 0..255 as text (the level of a sample by its value)
_LEVEL_TEXT = np.array([str(value) for value in range(256)])


def _joined(columns: list[np.ndarray]) -> np.ndarray:
    """The text columns joined with commas, row by row."""
    rows = columns[0]
    for column in columns[1:]:
        rows = np.char.add(np.char.add(rows, ","), column)
    return rows


def export_csv(path: str, session: CaptureSession, include_time: bool = False) -> None:
    """Export the capture as comma separated values: 0/1 per digital channel, the analog
    channels in their unit (volts)."""
    channels = [c for c in session.capture_channels if c.samples is not None]
    analog = [c for c in session.analog_channels if c.raw is not None]
    if not channels and not analog:
        raise ValueError("The capture contains no samples to export.")

    sample_count = min(int(c.samples.shape[0]) for c in channels) if channels else session.sample_count()
    header = [c.display_name for c in channels] + [f"{c.display_name} [{c.unit}]" for c in analog]
    if include_time:
        header.insert(0, "Time")
    if session.state_times is not None and len(session.state_times) >= sample_count:
        times = np.asarray(session.state_times[:sample_count], dtype=np.float64) / 1e6
    else:
        times = None  # computed block by block
    analog_values = [_analog_on_session_rate(session, channel, sample_count) for channel in analog]
    binary = not analog and not include_time and all(_is_binary(c.samples[:sample_count]) for c in channels)

    with atomic_open(path, newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerow(header)  # a name with a comma is quoted
        for start in range(0, sample_count, EXPORT_BLOCK):
            end = min(start + EXPORT_BLOCK, sample_count)
            if binary:
                # only 0 and 1: the characters of all rows as one table of bytes
                table = np.empty((end - start, 2 * len(channels)), dtype=np.uint8)
                table[:, 1::2] = ord(",")
                table[:, -1] = ord("\n")
                for index, channel in enumerate(channels):
                    table[:, 2 * index] = np.asarray(channel.samples[start:end], dtype=np.uint8) + ord("0")
                handle.write(table.tobytes().decode("ascii"))
                continue
            columns = []
            if include_time:
                block = times[start:end] if times is not None else \
                    (np.arange(start, end) - session.pre_trigger_samples) / float(session.frequency)
                columns.append(np.char.mod("%.12g", block))
            columns += [_LEVEL_TEXT[np.asarray(c.samples[start:end], dtype=np.uint8)] for c in channels]
            columns += [np.char.mod("%.6g", values[start:end]) for values in analog_values]
            handle.write("\n".join(_joined(columns).tolist()) + "\n")


_VCD_IDENTIFIERS = "!#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"


def export_vcd(path: str, session: CaptureSession) -> None:
    """Export the capture as a VCD file (PulseView, GTKWave, ...)."""
    channels = [c for c in session.capture_channels if c.samples is not None]
    analog = [c for c in session.analog_channels if c.raw is not None]
    if not channels and not analog:
        raise ValueError("The capture contains no samples to export.")

    sample_count = min(int(c.samples.shape[0]) for c in channels) if channels else session.sample_count()
    if sample_count <= 0:
        raise ValueError("The capture contains no samples to export.")
    frequency = max(int(session.frequency), 1)

    # A timescale at least as fine as the sample period. Times are computed from the sample
    # position (not by adding a rounded period), so a period like 41.67 ns at 24 MHz does not drift.
    period_ns = 1_000_000_000.0 / frequency
    if period_ns >= 1:
        timescale, units_per_second = "1 ns", 10**9
    elif period_ns >= 0.001:
        timescale, units_per_second = "1 ps", 10**12
    else:
        timescale, units_per_second = "1 fs", 10**15

    def timestamp(position: int) -> int:
        return (position * units_per_second + frequency // 2) // frequency

    # the times of a block at once, unless they do not fit into 64 bits (then one by one)
    fits = sample_count * units_per_second + frequency < 2**62

    def timestamps(positions: np.ndarray) -> np.ndarray:
        if fits:
            return ((positions * units_per_second + frequency // 2) // frequency).astype(str)
        return np.array([str(timestamp(int(position))) for position in positions])

    def identifier_of(index: int) -> str:
        identifier = _VCD_IDENTIFIERS[index % len(_VCD_IDENTIFIERS)]
        if index >= len(_VCD_IDENTIFIERS):
            identifier += _VCD_IDENTIFIERS[index // len(_VCD_IDENTIFIERS)]
        return identifier

    with atomic_open(path) as handle:
        handle.write("$date generated by openSciLab $end\n")
        handle.write("$version openSciLab $end\n")
        handle.write(f"$timescale {timescale} $end\n")
        handle.write("$scope module logic $end\n")

        identifiers = [identifier_of(index) for index in range(len(channels))]
        for identifier, channel in zip(identifiers, channels):
            name = (channel.display_name or channel.textual_channel_number).replace(" ", "_")
            handle.write(f"$var wire 1 {identifier} {name} $end\n")
        analog_identifiers = [identifier_of(len(channels) + offset) for offset in range(len(analog))]
        for identifier, channel in zip(analog_identifiers, analog):
            handle.write(f"$var real 64 {identifier} {channel.display_name.replace(' ', '_')} $end\n")

        handle.write("$upscope $end\n")
        handle.write("$enddefinitions $end\n")

        handle.write("#0\n")
        handle.write("$dumpvars\n")
        for identifier, channel in zip(identifiers, channels):
            handle.write(f"{int(channel.samples[0])}{identifier}\n")
        analog_values = [_analog_on_session_rate(session, channel, sample_count) for channel in analog]
        for identifier, values in zip(analog_identifiers, analog_values):
            # (a value that is no number – past the end of a slower channel – is none in a VCD)
            first = values[0] if len(values) and np.isfinite(values[0]) else 0.0
            handle.write(f"r{first:.6g} {identifier}\n")
        handle.write("$end\n")

        # Only transitions are written, which keeps the file small. Block by block: the changes of
        # all channels in a block, in the order of their positions.
        for start in range(1, sample_count, EXPORT_BLOCK):
            end = min(start + EXPORT_BLOCK, sample_count)
            positions, texts = [], []
            for identifier, channel in zip(identifiers, channels):
                part = np.asarray(channel.samples[start - 1 : end], dtype=np.uint8)
                changes = np.flatnonzero(part[1:] != part[:-1])
                if len(changes):
                    positions.append(changes + start)
                    texts.append(np.char.add(_LEVEL_TEXT[part[changes + 1]], identifier))
            for identifier, values in zip(analog_identifiers, analog_values):
                part = values[start - 1 : end]
                changes = np.flatnonzero((part[1:] != part[:-1]) & np.isfinite(part[1:]))
                if len(changes):
                    positions.append(changes + start)
                    texts.append(np.char.add(np.char.add("r", np.char.mod("%.6g", part[changes + 1])),
                                             f" {identifier}"))
            if not positions:
                continue
            position = np.concatenate(positions)
            text = np.concatenate(texts)
            order = np.argsort(position, kind="stable")  # channels stay in their order at one time
            position, text = position[order], text[order]
            new_time = np.concatenate(([True], position[1:] != position[:-1]))
            stamps = np.char.add(np.char.add("#", timestamps(position[new_time])), "\n")
            lines = text.astype(object)
            lines[new_time] = stamps.astype(object) + lines[new_time]
            handle.write("\n".join(lines.tolist()) + "\n")

        handle.write(f"#{timestamp(sample_count)}\n")
