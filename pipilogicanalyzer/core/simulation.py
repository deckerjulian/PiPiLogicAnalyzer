# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test signals of the simulated capture (trigger type 4).

Python twin of ``firmware/PiPiLogicAnalyzer/PiPiLogicAnalyzer_Simulation.c``: the
board generates these samples itself when its firmware supports simulation,
otherwise the application computes them. Both implementations must produce
identical samples (``tests/test_simulation.py`` compiles the C file and
compares them).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

import numpy as np


class SimulationPattern(IntEnum):
    COUNTER = 0
    WALKING_ONE = 1
    PROTOCOLS = 2

    @property
    def label(self) -> str:
        return {
            SimulationPattern.COUNTER: "Counter",
            SimulationPattern.WALKING_ONE: "Walking one",
            SimulationPattern.PROTOCOLS: "Protocols (UART, SPI, I2C)",
        }[self]


MESSAGE = b"PiPiLogicAnalyzer\n"
WALK_STEP = 16
UART_BIT = 10
UART_IDLE = 200
SPI_HALF = 5
SPI_GAP = 100
I2C_QUARTER = 5
I2C_GAP = 100
I2C_ADDRESS = 0x50

_MESSAGE = np.frombuffer(MESSAGE, dtype=np.uint8).astype(np.int64)


def _uart(t_index: np.ndarray) -> np.ndarray:
    period = UART_IDLE + len(MESSAGE) * 10 * UART_BIT
    t = t_index % period
    bit = np.maximum(t - UART_IDLE, 0) // UART_BIT
    position = bit % 10
    byte = _MESSAGE[np.minimum(bit // 10, len(MESSAGE) - 1)]
    data = (byte >> np.clip(position - 1, 0, 7)) & 1
    level = np.where(position == 0, 0, np.where(position == 9, 1, data))
    return np.where(t < UART_IDLE, 1, level)


def _spi_bit(bit: np.ndarray) -> np.ndarray:
    return (_MESSAGE[bit // 8] >> (7 - bit % 8)) & 1


def _spi(index: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    bits = len(MESSAGE) * 8
    cell = 2 * SPI_HALF
    period = SPI_GAP + SPI_HALF + bits * cell + SPI_HALF
    t = index % period
    idle = t < SPI_GAP
    u = t - SPI_GAP
    lead = u < SPI_HALF
    data_region = (u >= SPI_HALF) & (u < SPI_HALF + bits * cell)
    v = np.clip(u - SPI_HALF, 0, bits * cell - 1)

    clk = np.where(data_region, (v % cell) >= SPI_HALF, False).astype(np.int64)
    mosi = np.where(lead, _spi_bit(np.zeros_like(v)), np.where(data_region, _spi_bit(v // cell), 0))
    cs = idle.astype(np.int64)
    return np.where(idle, 0, clk), np.where(idle, 0, mosi), cs


def _i2c_bit(cell: np.ndarray) -> np.ndarray:
    byte_index = cell // 9
    position = cell % 9
    address = (I2C_ADDRESS << 1) & 0xFF
    value = np.where(byte_index == 0, address, _MESSAGE[np.clip(byte_index - 1, 0, len(MESSAGE) - 1)])
    data = (value >> np.clip(7 - position, 0, 7)) & 1
    return np.where(position == 8, 0, data)


def _i2c(index: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cell = 4 * I2C_QUARTER
    cells = 9 * (len(MESSAGE) + 1)
    period = I2C_GAP + cell + cells * cell + cell
    t = index % period

    idle = t < I2C_GAP
    u = t - I2C_GAP
    start = (u >= 0) & (u < cell)
    w = u - cell
    data_region = (w >= 0) & (w < cells * cell)

    safe = np.clip(w, 0, cells * cell - 1)
    current = safe // cell
    phase = safe % cell
    previous = np.where(current == 0, 0, _i2c_bit(np.maximum(current - 1, 0)))
    data_scl = (phase >= 2 * I2C_QUARTER).astype(np.int64)
    data_sda = np.where(phase < I2C_QUARTER, previous, _i2c_bit(current))

    stop_phase = w - cells * cell
    stop_scl = (stop_phase >= 2 * I2C_QUARTER).astype(np.int64)
    stop_sda = (stop_phase >= 3 * I2C_QUARTER).astype(np.int64)

    scl = np.where(idle | start, 1, np.where(data_region, data_scl, stop_scl))
    sda = np.where(
        idle,
        1,
        np.where(start, (u < 2 * I2C_QUARTER).astype(np.int64), np.where(data_region, data_sda, stop_sda)),
    )
    return scl, sda


def _counter(index: np.ndarray, shift: int) -> np.ndarray:
    return (index >> (shift % 16)) & 1


def generate(pattern: int, channel_count: int, sample_count: int) -> list[np.ndarray]:
    """Samples (``uint8`` 0/1 arrays) of every channel of a simulated capture."""
    pattern = int(pattern)
    channel_count = max(0, min(int(channel_count), 32))
    index = np.arange(int(sample_count), dtype=np.int64)

    if pattern == SimulationPattern.WALKING_ONE:
        active = (index // WALK_STEP) % max(channel_count, 1)
        levels = [active == channel for channel in range(channel_count)]
    elif pattern == SimulationPattern.PROTOCOLS:
        clk, mosi, cs = _spi(index)
        scl, sda = _i2c(index)
        levels = [_uart(index), clk, mosi, cs, scl, sda]
        levels += [_counter(index, channel - 6) for channel in range(6, channel_count)]
        levels = levels[:channel_count]
    else:
        levels = [_counter(index, channel) for channel in range(channel_count)]

    return [np.asarray(level, dtype=np.uint8) for level in levels]


def sample_masks(pattern: int, channel_count: int, sample_count: int) -> np.ndarray:
    """Samples packed like the firmware does (bit n = n-th channel)."""
    masks = np.zeros(int(sample_count), dtype=np.uint64)
    for channel, samples in enumerate(generate(pattern, channel_count, sample_count)):
        masks |= samples.astype(np.uint64) << np.uint64(channel)
    return masks


def describe(pattern: int, channel_count: int, frequency: int) -> str:
    """Human readable description of the channels of a pattern."""
    pattern = SimulationPattern(int(pattern))
    if pattern == SimulationPattern.COUNTER:
        return "Channel n toggles every 2^n samples (binary counter, repeats every 16 channels)."
    if pattern == SimulationPattern.WALKING_ONE:
        return f"One channel is high at a time for {WALK_STEP} samples, in channel order."

    frequency = max(int(frequency), 1)
    lines = [
        f"Channel 1: UART TX, {frequency // UART_BIT:,} baud, 8N1",
        f"Channels 2-4: SPI CLK, MOSI, CS (mode 0, {frequency // (2 * SPI_HALF):,} Hz clock)",
        f"Channels 5-6: I2C SCL, SDA ({frequency // (4 * I2C_QUARTER):,} bit/s, "
        f"write to 0x{I2C_ADDRESS:02X})",
    ]
    if channel_count > 6:
        lines.append("Further channels: binary counter")
    return "\n".join(lines) + f'\nAll protocols carry the text "{MESSAGE.decode().strip()}".'


def decoder_configuration(pattern: int, channel_count: int, frequency: int, registry: Any) -> list[dict]:
    """Decoder list (``SigrokProvider.to_list`` format) matching the protocol pattern."""
    from ..sigrok.provider import DecoderInstance

    if int(pattern) != SimulationPattern.PROTOCOLS:
        return []

    wanted = (
        ("uart", {"rx": 0}, {"baudrate": max(int(frequency) // UART_BIT, 1), "format": "ascii"}),
        ("spi", {"clk": 1, "mosi": 2, "cs": 3}, {}),
        ("i2c", {"scl": 4, "sda": 5}, {}),
    )
    configuration = []
    for decoder_id, channels, options in wanted:
        info = registry.get(decoder_id)
        if info is None or max(channels.values()) >= channel_count:
            continue
        indexes = {channel.id: channel.index for channel in info.channels}
        if any(name not in indexes for name in channels):
            continue
        instance = DecoderInstance(
            decoder_id=info.id,
            label=f"{info.name} (simulated)",
            channel_map={indexes[name]: capture for name, capture in channels.items()},
            options={**info.default_options(), **options},
            color_index=len(configuration),
        )
        data = instance.to_dict()
        data["parent"] = None
        configuration.append(data)
    return configuration
