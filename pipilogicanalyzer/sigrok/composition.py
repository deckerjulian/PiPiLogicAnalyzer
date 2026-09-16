# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""How a decoded value is composed of the channel levels.

When the pointer rests on an annotation, the waveform shows every channel the decoder reads
with its level at the sample the decoder took the value from. Numbered channels such as
``A0``…``A15`` or ``D0``…``D7`` form a bus: their bits add up to the decoded address or data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..driver.models import AnalyzerChannel
from .engine import AnnotationSegment, DecoderChannel, DecoderInfo
from .provider import DecoderInstance

_NUMBERED_ID = re.compile(r"^(?P<prefix>.*?)(?P<bit>\d+)$")
_NAME_PREFIX = re.compile(r"^(?P<prefix>\D+?)\d+$")


def short_name(channel: DecoderChannel) -> str:
    """``A15 (F)`` → ``A15``: the channel name without the pin in brackets."""
    return channel.name.split(" (", 1)[0].strip() or channel.id


@dataclass
class ChannelBit:
    """Level of one decoder channel at the sample point."""

    channel: DecoderChannel
    capture_index: int
    level: int
    #: Bus the channel belongs to and its bit number there (``None`` for single lines).
    bus: Optional[str] = None
    bit: Optional[int] = None
    #: The level differs one sample before or after the sample point.
    changing: bool = False

    @property
    def name(self) -> str:
        return short_name(self.channel)

    @property
    def label(self) -> str:
        text = f"{self.name} = {self.level}"
        if self.bit is not None and self.level:
            text += f" (+${1 << self.bit:X})"
        if self.changing:
            text += " ~"
        return text


@dataclass
class Bus:
    """Numbered channels forming one value, most significant bit first."""

    name: str
    bits: list[ChannelBit] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max(bit.bit for bit in self.bits) + 1

    @property
    def value(self) -> int:
        return sum(bit.level << bit.bit for bit in self.bits)

    @property
    def binary(self) -> str:
        """Bits from the most significant one, in groups of four."""
        levels = {bit.bit: str(bit.level) for bit in self.bits}
        digits = "".join(levels.get(position, "?") for position in reversed(range(self.width)))
        padding = (-len(digits)) % 4
        digits = " " * padding + digits
        return " ".join(digits[index:index + 4] for index in range(0, len(digits), 4)).strip()

    @property
    def hexadecimal(self) -> str:
        return f"${self.value:0{(self.width + 3) // 4}X}"

    def describe(self) -> str:
        return f"{self.bits[0].name}…{self.bits[-1].name} = {self.binary} = {self.hexadecimal}"


@dataclass
class Composition:
    sample: int
    bits: list[ChannelBit]
    buses: list[Bus]

    def bus_of(self, bit: ChannelBit) -> Optional[Bus]:
        return next((bus for bus in self.buses if bit in bus.bits), None)

    def describe(self) -> str:
        lines = [bus.describe() for bus in self.buses]
        singles = [bit for bit in self.bits if bit.bus is None]
        if singles:
            lines.append(" · ".join(f"{bit.name} = {bit.level}" for bit in singles))
        changing = [bit.name for bus in self.buses for bit in bus.bits if bit.changing]
        if changing:
            lines.append("Changing next to the read point: " + ", ".join(changing))
        return "\n".join(lines)


def sample_point(segment: AnnotationSegment) -> int:
    if segment.sample_point is not None:
        return segment.sample_point
    return (segment.first_sample + max(segment.last_sample, segment.first_sample)) // 2


def compose(
    info: DecoderInfo,
    instance: DecoderInstance,
    channels: Sequence[AnalyzerChannel],
    segment: AnnotationSegment,
) -> Optional[Composition]:
    """Levels of the decoder channels at the sample point of ``segment``."""
    lengths = [channel.samples.size for channel in channels if channel.samples is not None]
    if not lengths or not instance.channel_map:
        return None
    sample = min(max(sample_point(segment), 0), min(lengths) - 1)

    by_index = {channel.index: channel for channel in info.channels}
    bits: list[ChannelBit] = []
    for decoder_index, capture_index in sorted(instance.channel_map.items()):
        channel = by_index.get(decoder_index)
        if channel is None or not 0 <= capture_index < len(channels):
            continue
        samples = channels[capture_index].samples
        if samples is None:
            continue
        level = int(samples[sample]) & 1
        neighbours = [int(samples[position]) & 1 for position in (sample - 1, sample + 1)
                      if 0 <= position < samples.size]
        bits.append(ChannelBit(channel, capture_index, level, changing=any(value != level for value in neighbours)))

    # A bus needs at least two channels numbered 0, 1, 2, ... with the same prefix
    # (so /IO1 and /IO2 stay single lines).
    candidates: dict[str, list[tuple[int, ChannelBit]]] = {}
    for bit in bits:
        match = _NUMBERED_ID.match(bit.channel.id)
        if match and match.group("prefix"):
            candidates.setdefault(match.group("prefix"), []).append((int(match.group("bit")), bit))

    buses: list[Bus] = []
    for prefix, members in candidates.items():
        numbers = sorted(number for number, _ in members)
        if len(members) < 2 or numbers != list(range(len(numbers))):
            continue
        name_match = _NAME_PREFIX.match(members[0][1].name)
        bus = Bus(name_match.group("prefix") if name_match else prefix.upper())
        for number, bit in sorted(members, key=lambda item: -item[0]):
            bit.bus, bit.bit = bus.name, number
            bus.bits.append(bit)
        buses.append(bus)

    return Composition(sample=sample, bits=bits, buses=buses)
