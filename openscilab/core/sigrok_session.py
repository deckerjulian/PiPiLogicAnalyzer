# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""sigrok session files (``.sr``), as written and read by sigrok-cli and PulseView.

A session file is a zip archive (format version 2 of libsigrok's ``srzip`` output):

``version``
    The text ``2`` (stored uncompressed).
``metadata``
    A GLib key file::

        [global]
        sigrok version=0.5.2

        [device 1]
        capturefile=logic-1
        total probes=3
        samplerate=10 MHz
        probe1=CLK
        probe2=DATA
        probe3=CS
        unitsize=1

``logic-1-1``, ``logic-1-2``, ...
    The samples, ``unitsize`` bytes per sample in little endian order: bit 0 of the first byte is
    ``probe1``. libsigrok starts a new chunk for every 4 MiB; readers concatenate the chunks in
    the order of their numbers. Version 1 files hold all samples in a single ``logic-1``.

The trigger position is not part of the libsigrok format (sigrok marks it in the data stream
only). It is written as ``trigger pos`` (the name DSView uses), which libsigrok ignores, and read
from ``trigger pos`` or ``trigger`` if present. The channel numbers of the capture are kept in
``channel numbers`` (comma separated, in probe order); libsigrok ignores unknown keys (keys must
not start with ``probe`` or ``analog``, which it parses).
"""

from __future__ import annotations

import configparser
import os
import re
import time
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence

import numpy as np

from ..driver.models import AnalyzerChannel, CaptureSession
from .files import atomic_open

#: Bytes of one ``logic-1-N`` chunk (``CHUNK_SIZE`` of libsigrok's srzip output)
CHUNK_BYTES = 4 * 1024 * 1024
#: ``sigrok version`` written to the metadata (readers do not check it)
SIGROK_VERSION = "0.5.2"
#: Samples unpacked at once while reading or packed at once while writing
_BLOCK_SAMPLES = 1 << 20

_PREFIXES = ("", "k", "M", "G", "T", "P", "E")
_MULTIPLIERS = {"k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12, "p": 10**15, "e": 10**18}
_RATE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*([kKmMgGtTpPeE]?)\s*(?:[hH][zZ])?\s*$")
_TEXTUAL_CHANNEL_RE = re.compile(r"^Channel (\d+)$")


class SigrokSessionError(ValueError):
    """The file is not a sigrok session or holds no logic samples."""


# ------------------------------------------------------------------- samplerate
def samplerate_string(rate: int) -> str:
    """``rate`` the way libsigrok writes it (``sr_samplerate_string``): ``"10 MHz"``,
    ``"1.5 kHz"``, ``"500 Hz"``; exact, without rounding (``"1.234567 MHz"``)."""
    rate = int(rate)
    index = 0
    while index + 1 < len(_PREFIXES) and rate // 1000**index >= 1000:
        index += 1
    divisor = 1000**index
    whole, rest = divmod(rate, divisor)
    fraction = ""
    if index and rest:
        fraction = "." + f"{rest:0{index * 3}d}".rstrip("0")
    return f"{whole}{fraction} {_PREFIXES[index]}Hz"


def parse_samplerate(text: str) -> int:
    """Rate of a ``samplerate`` value (``sr_parse_sizestring``): ``"10 MHz"``, ``"1.5 kHz"``,
    ``"24000000"``; ``m``/``M`` both mean mega, as in libsigrok."""
    match = _RATE_RE.match(text or "")
    if not match:
        raise SigrokSessionError(f"Invalid samplerate '{text}'.")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as error:  # pragma: no cover - excluded by the expression
        raise SigrokSessionError(f"Invalid samplerate '{text}'.") from error
    suffix = match.group(2).lower()
    return int(number * _MULTIPLIERS.get(suffix, 1))


def unit_size(channel_count: int) -> int:
    """Bytes per sample: 1, 2, 4 or 8 like the devices of libsigrok, more beyond 64 channels."""
    size = max(1, (channel_count + 7) // 8)
    for candidate in (1, 2, 4, 8):
        if size <= candidate:
            return candidate
    return size


# ---------------------------------------------------------------------- export
def save_session(path: str, session: CaptureSession) -> None:
    """Write ``session`` as a sigrok session file.

    The samples are packed and compressed block by block, so memory-mapped captures of any size
    are written without holding them in memory twice.
    """
    channels = [channel for channel in session.capture_channels if channel.samples is not None]
    analog = [channel for channel in session.analog_channels if channel.raw is not None]
    if not channels and not analog:
        raise ValueError("The capture contains no samples to export.")
    sample_count = min(int(channel.samples.shape[0]) for channel in channels) if channels else session.sample_count()
    size = unit_size(max(len(channels), 1))

    lines = [
        "[global]",
        f"sigrok version={SIGROK_VERSION}",
        "",
        "[device 1]",
    ]
    if channels:
        lines.append("capturefile=logic-1")
    lines += [
        f"total probes={len(channels)}",
        f"samplerate={samplerate_string(max(int(session.frequency), 1))}",
        f"total analog={len(analog)}",
    ]
    for index, channel in enumerate(channels):
        lines.append(f"probe{index + 1}={_key_file_escape(channel.display_name)}")
    # Analog channels are numbered after the logic channels (as libsigrok does).
    for offset, channel in enumerate(analog):
        lines.append(f"analog{len(channels) + offset + 1}={_key_file_escape(channel.display_name)}")
    lines.append(f"unitsize={size}")
    if 0 < session.pre_trigger_samples < sample_count:
        lines.append(f"trigger pos={int(session.pre_trigger_samples)}")
    lines.append("channel numbers=" + ",".join(str(int(c.channel_number)) for c in channels))
    metadata = "\n".join(lines) + "\n"

    chunk_samples = CHUNK_BYTES // size
    with atomic_open(path, "wb") as target, \
            zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("version"), "2", compress_type=zipfile.ZIP_STORED)
        archive.writestr("metadata", metadata)
        chunk = 1
        for start in range(0, sample_count if channels else 0, chunk_samples):
            end = min(start + chunk_samples, sample_count)
            info = zipfile.ZipInfo(f"logic-1-{chunk}", date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, "w", force_zip64=True) as handle:
                for block in range(start, end, _BLOCK_SAMPLES):
                    handle.write(_pack(channels, block, min(block + _BLOCK_SAMPLES, end), size))
            chunk += 1
        from .capture_io import _analog_on_session_rate

        analog_chunk = CHUNK_BYTES // 4
        for offset, channel in enumerate(analog):
            number = len(channels) + offset + 1
            values = _analog_on_session_rate(session, channel, sample_count).astype("<f4")
            for part, start in enumerate(range(0, sample_count, analog_chunk), start=1):
                info = zipfile.ZipInfo(f"analog-1-{number}-{part}", date_time=time.localtime()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, values[start:start + analog_chunk].tobytes())


def _pack(channels: Sequence[AnalyzerChannel], start: int, end: int, size: int) -> bytes:
    packed = np.zeros((end - start, size), dtype=np.uint8)
    for index, channel in enumerate(channels):
        samples = np.asarray(channel.samples[start:end], dtype=np.uint8) & 1
        packed[:, index // 8] |= samples << np.uint8(index % 8)
    return packed.tobytes()


def _key_file_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def _key_file_unescape(text: str) -> str:
    replacements = {"\\": "\\", "n": "\n", "t": "\t", "r": "\r", "s": " "}
    return re.sub(r"\\(.)", lambda match: replacements.get(match.group(1), match.group(1)), text)


# ---------------------------------------------------------------------- import
def load_session(path: str) -> CaptureSession:
    """Read the logic channels of a sigrok session file."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise SigrokSessionError(f"{os.path.basename(path)} is not a sigrok session file.") from error

    with archive:
        names = set(archive.namelist())
        if "version" not in names or "metadata" not in names:
            raise SigrokSessionError(f"{os.path.basename(path)} is not a sigrok session file.")
        version = archive.read("version").decode("ascii", "replace").strip()
        if version not in ("1", "2"):
            raise SigrokSessionError(f"Unsupported sigrok session format version {version!r}.")

        parser = configparser.ConfigParser(
            delimiters=("=",), comment_prefixes=("#",), interpolation=None, strict=False
        )
        parser.optionxform = str  # keep the case of the keys
        try:
            parser.read_string(archive.read("metadata").decode("utf-8", "replace"))
        except configparser.Error as error:  # (a damaged session file)
            raise SigrokSessionError(f"{os.path.basename(path)}: its metadata cannot be read ({error}).") from None
        section = _logic_device(parser)
        if section is None:
            analog_only = _analog_device(parser)
            if analog_only is None:
                raise SigrokSessionError("The session file contains no logic samples.")
            return _analog_only_session(archive, names, parser[analog_only])
        device = parser[section]

        capture = device.get("capturefile", "logic-1").strip()
        size = int(device.get("unitsize", "1"))
        if size < 1:
            raise SigrokSessionError(f"Invalid unitsize {size}.")
        probes: list[tuple[int, str]] = []
        for key, value in device.items():
            match = re.fullmatch(r"probe(\d+)", key)
            if match:
                probes.append((int(match.group(1)) - 1, _key_file_unescape(value.strip())))
        probes.sort()
        probes = [(bit, name) for bit, name in probes if 0 <= bit < size * 8]
        if not probes:
            raise SigrokSessionError("The session file names no logic channels.")

        chunks = _chunk_names(names, capture)
        if not chunks:
            raise SigrokSessionError(f"The session file contains no data ('{capture}').")
        total_bytes = sum(archive.getinfo(name).file_size for name in chunks)
        sample_count = total_bytes // size

        arrays = [np.empty(sample_count, dtype=np.uint8) for _ in probes]
        position = 0
        remainder = b""
        for name in chunks:
            with archive.open(name) as handle:
                while position < sample_count:
                    data = handle.read(_BLOCK_SAMPLES * size)
                    if not data:
                        break
                    data = remainder + data
                    usable = len(data) - len(data) % size
                    remainder = data[usable:]
                    rows = np.frombuffer(data, dtype=np.uint8, count=usable).reshape(-1, size)
                    rows = rows[: sample_count - position]
                    for (bit, _), array in zip(probes, arrays):
                        array[position:position + len(rows)] = (rows[:, bit // 8] >> (bit % 8)) & 1
                    position += len(rows)

        rate = device.get("samplerate")
        frequency = parse_samplerate(rate) if rate else 0
        trigger = device.get("trigger pos", device.get("trigger"))
        numbers = _channel_numbers(device.get("channel numbers"), len(probes))
        analog = _read_analog(archive, names, device)

    pre_trigger = 0
    if trigger is not None:
        try:
            pre_trigger = min(max(int(trigger.strip()), 0), position)
        except ValueError:
            pre_trigger = 0

    session = CaptureSession(
        frequency=max(frequency, 1),
        pre_trigger_samples=pre_trigger,
        post_trigger_samples=position - pre_trigger,
        loop_count=0,
    )
    session.capture_channels = _channels(probes, [array[:position] for array in arrays], numbers)
    session.analog_channels = analog
    return session


def _analog_device(parser: configparser.ConfigParser) -> Optional[str]:
    for section in parser.sections():
        if section.startswith("device ") and any(key.startswith("analog") and key[6:].isdigit()
                                                 for key in parser[section]):
            return section
    return None


def _read_analog(archive: zipfile.ZipFile, names: set[str], device) -> list:
    """The analog channels of a session file (``analog-1-<n>-<chunk>``, float32)."""
    from ..driver.models import AnalogChannel

    channels = []
    entries = sorted(((int(key[6:]), _key_file_unescape(value.strip())) for key, value in device.items()
                      if key.startswith("analog") and key[6:].isdigit()))
    for index, (number, name) in enumerate(entries):
        parts = []
        part = 1
        while f"analog-1-{number}-{part}" in names:
            parts.append(np.frombuffer(archive.read(f"analog-1-{number}-{part}"), dtype="<f4"))
            part += 1
        if not parts:
            continue
        values = np.concatenate(parts).astype(np.float64)
        channels.append(AnalogChannel.from_volts(values, channel_number=index, channel_name=name))
    return channels


def _analog_only_session(archive: zipfile.ZipFile, names: set[str], device) -> CaptureSession:
    rate = device.get("samplerate")
    session = CaptureSession(frequency=max(parse_samplerate(rate) if rate else 1, 1), pre_trigger_samples=0,
                             post_trigger_samples=0, loop_count=0)
    session.analog_channels = _read_analog(archive, names, device)
    session.post_trigger_samples = session.sample_count()
    return session


def _logic_device(parser: configparser.ConfigParser) -> Optional[str]:
    for section in parser.sections():
        if section.startswith("device ") and parser[section].get("capturefile"):
            return section
    return None


def _chunk_names(names: set[str], capture: str) -> list[str]:
    numbered = []
    index = 1
    while f"{capture}-{index}" in names:
        numbered.append(f"{capture}-{index}")
        index += 1
    if numbered:
        return numbered
    return [capture] if capture in names else []


def _channel_numbers(text: Optional[str], count: int) -> Optional[list[int]]:
    """The ``channel numbers`` written by this application, if they fit the probes."""
    if not text:
        return None
    try:
        numbers = [int(item) for item in text.split(",")]
    except ValueError:
        return None
    if len(numbers) != count or len(set(numbers)) != count or min(numbers) < 0:
        return None
    return numbers


def _channels(
    probes: list[tuple[int, str]], arrays: list[np.ndarray], numbers: Optional[list[int]] = None
) -> list[AnalyzerChannel]:
    """Channels named like the probes.

    Without ``numbers`` a probe named ``Channel N`` (as this application names unnamed channels)
    becomes channel ``N - 1``, the others are numbered by their bit (unless taken).
    """
    channels = []
    if numbers is None:
        textual = [_TEXTUAL_CHANNEL_RE.match(name) for _, name in probes]
        wanted = [int(match.group(1)) - 1 if match else bit for (bit, _), match in zip(probes, textual)]
        numbers = wanted if len(set(wanted)) == len(wanted) else [bit for bit, _ in probes]
    for (bit, name), number, samples in zip(probes, numbers, arrays):
        textual_name = f"Channel {number + 1}"
        channels.append(
            AnalyzerChannel(
                channel_number=number,
                channel_name="" if name == textual_name else name,
                samples=samples,
            )
        )
    return channels
