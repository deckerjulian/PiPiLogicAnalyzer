#!/usr/bin/env python3
# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate ``examples/demo.lac``: a synthetic I2C + UART capture.

Useful to try the application (and the protocol decoders) without hardware::

    python tools/make_demo_capture.py
    pipilogicanalyzer examples/demo.lac
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipilogicanalyzer.core import capture_io  # noqa: E402
from pipilogicanalyzer.core.regions import SampleRegion  # noqa: E402
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession  # noqa: E402

SAMPLE_RATE = 1_000_000


def i2c_transfers(transfers, quarter: int = 10) -> tuple[np.ndarray, np.ndarray]:
    """Build SCL/SDA for a list of ``(address, [bytes])`` write transfers."""
    scl: list[int] = []
    sda: list[int] = []

    def emit(clock: int, data: int, length: int = quarter) -> None:
        scl.extend([clock] * length)
        sda.extend([data] * length)

    emit(1, 1, quarter * 4)
    for address, payload in transfers:
        emit(1, 1)
        emit(1, 0)  # start condition
        for byte in [address << 1, *payload]:
            for bit in range(7, -1, -1):
                value = (byte >> bit) & 1
                emit(0, value)
                emit(1, value)
            emit(0, 0)  # ACK
            emit(1, 0)
        emit(0, 0)
        emit(1, 0)
        emit(1, 1)  # stop condition
        emit(1, 1, quarter * 4)
    emit(1, 1, quarter * 8)
    return np.array(scl, dtype=np.uint8), np.array(sda, dtype=np.uint8)


def uart_stream(text: bytes, baud_rate: int, length: int) -> np.ndarray:
    """Build an 8N1 UART line of ``length`` samples."""
    samples_per_bit = SAMPLE_RATE / baud_rate
    line: list[int] = []
    for byte in text:
        bits = [0] + [(byte >> index) & 1 for index in range(8)] + [1, 1]
        for bit in bits:
            line.extend([bit] * int(round(samples_per_bit)))

    stream = np.ones(length, dtype=np.uint8)
    start = min(200, max(length - len(line) - 1, 0))
    stream[start : start + len(line)] = np.array(line[: length - start], dtype=np.uint8)
    return stream


def main() -> int:
    scl, sda = i2c_transfers([(0x50, [0x00, 0x42]), (0x3C, [0xAE, 0xA1])])
    length = scl.size

    session = CaptureSession(
        frequency=SAMPLE_RATE,
        pre_trigger_samples=40,
        post_trigger_samples=length - 40,
    )
    session.capture_channels = [
        AnalyzerChannel(channel_number=0, channel_name="SCL", samples=scl),
        AnalyzerChannel(channel_number=1, channel_name="SDA", samples=sda),
        AnalyzerChannel(
            channel_number=2, channel_name="RX", samples=uart_stream(b"PiPiLogicAnalyzer", 115200, length)
        ),
        AnalyzerChannel(
            channel_number=3,
            channel_name="CLK",
            samples=((np.arange(length) // 8) % 2).astype(np.uint8),
        ),
    ]

    regions = [
        SampleRegion(
            first_sample=40,
            last_sample=min(length - 1, 640),
            region_name="EEPROM write",
        )
    ]

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "examples", "demo.lac")
    capture_io.save_capture(path, session, regions)
    print(f"wrote {path} ({length} samples, {len(session.capture_channels)} channels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
