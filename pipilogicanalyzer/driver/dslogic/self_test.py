# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Board self-test of the DSLogic analyzers.

Most checks capture the test counter of the FPGA (``INT_TEST_BIT``) instead of the inputs: sample
n holds n on channels 0..15, so the capture memory, the USB transfer and the trigger are checked
bit by bit without any signal. Only the input check looks at the inputs, which read low with the
probes disconnected.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import numpy as np

from ..base import CaptureCompletedArgs, CaptureError, DeviceConnectionError, SelfTestResult
from ..models import AnalyzerChannel, CaptureSession, TriggerType
from . import protocol
from .protocol import AcquisitionMode

if TYPE_CHECKING:
    from .driver import DSLogicDriver

#: Channels the test counter covers
COUNTER_CHANNELS = 16
#: DSView runs its internal test at 100 MHz; there the counter advances once per sample
TEST_RATE = 100_000_000
BUFFER_SAMPLES = 4 << 20
#: Stream check: this long at the highest stream rate of 16 channels (at most TEST_RATE)
STREAM_SECONDS = 0.25
TRIGGER_PATTERN = 0xA5
TRIGGER_EDGE_CHANNEL = 4
INPUT_RATE = 10_000_000
INPUT_SAMPLES = 100_000
CAPTURE_TIMEOUT = 30.0


class _CaptureFailed(Exception):
    pass


def run_self_test(driver: "DSLogicDriver") -> list[SelfTestResult]:
    """Runs every check; no probes may be connected for the input check."""
    if driver.is_capturing:
        raise DeviceConnectionError("The device is capturing.")
    results = [_firmware(driver), _fpga(driver), _usb(driver)]
    if driver.profile.security:
        results.append(_security(driver))
    for check in (_buffer_capture, _stream_capture, _pattern_trigger, _edge_trigger):
        try:
            results.append(check(driver))
        except _CaptureFailed as error:
            results.append(SelfTestResult(check.__name__.strip("_").upper(), "FAIL", str(error)))
    results.extend(_inputs(driver))
    return results


# ------------------------------------------------------------------ helpers
def _capture(
    driver: "DSLogicDriver",
    rate: int,
    samples: int,
    channels: int,
    mode: AcquisitionMode = AcquisitionMode.BUFFER,
    internal_test: bool = True,
    **trigger,
) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=0, post_trigger_samples=samples)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in range(channels)]
    session.acquisition_mode = mode.value
    session.trigger_type = TriggerType.IMMEDIATE
    for name, value in trigger.items():
        setattr(session, name, value)

    done = threading.Event()
    outcome: list[CaptureCompletedArgs] = []

    def completed(args: CaptureCompletedArgs) -> None:
        outcome.append(args)
        done.set()

    error = driver.start_capture(session, completed, internal_test=internal_test)
    if error != CaptureError.NONE:
        raise _CaptureFailed(f"The capture could not be started ({error.value}).")
    if not done.wait(CAPTURE_TIMEOUT):
        driver.stop_capture()
        raise _CaptureFailed("The capture did not finish (no data or no trigger).")
    if not outcome[0].success:
        raise _CaptureFailed(f"The capture failed: {outcome[0].error}")
    return session


def _counter(session: CaptureSession) -> np.ndarray:
    """The samples of channels 0..15 as one word per sample."""
    word = np.zeros(len(session.capture_channels[0].samples), dtype=np.uint32)
    for channel in session.capture_channels:
        if channel.channel_number < COUNTER_CHANNELS:
            word |= channel.samples.astype(np.uint32) << channel.channel_number
    return word


def _counter_errors(word: np.ndarray, bits: int) -> tuple[int, list[int]]:
    """(wrong samples, channels with wrong bits) of a counter that should advance by one."""
    mask = (1 << bits) - 1
    expected = (np.uint32(word[0]) + np.arange(len(word), dtype=np.uint32)) & mask
    wrong = (word & mask) ^ expected
    channels = [number for number in range(bits) if np.any((wrong >> number) & 1)]
    return int(np.count_nonzero(wrong)), channels


def _channel_list(channels: list[int]) -> str:
    return ", ".join(str(number + 1) for number in channels)


def _counter_result(item: str, session: CaptureSession, what: str, bits: int) -> SelfTestResult:
    word = _counter(session)
    wrong, channels = _counter_errors(word, bits)
    if wrong:
        return SelfTestResult(
            item, "FAIL", f"{wrong:,} of {len(word):,} samples wrong, channels {_channel_list(channels)}"
        )
    return SelfTestResult(item, "OK", f"{len(word):,} samples {what}, test pattern exact")


# ------------------------------------------------------------------- checks
def _firmware(driver: "DSLogicDriver") -> SelfTestResult:
    major, minor = driver.firmware_version
    return SelfTestResult("FIRMWARE", "OK", f"Version {major}.{minor} (DSView protocol {major})")


def _fpga(driver: "DSLogicDriver") -> SelfTestResult:
    if not driver.read_status() & protocol.FPGA_DONE:
        return SelfTestResult("FPGA", "FAIL", "The FPGA is not configured")
    return SelfTestResult("FPGA", "OK", f"{driver.profile.bitstream}, version 0x{driver.hdl_version:02X}")


def _usb(driver: "DSLogicDriver") -> SelfTestResult:
    if driver.info.super_speed:
        return SelfTestResult("USB", "OK", "USB 3 (SuperSpeed)")
    if driver.profile.usb3:
        return SelfTestResult(
            "USB", "SLOW", "Connected with USB 2: the stream rates are lower, use a USB 3 port and cable"
        )
    return SelfTestResult("USB", "OK", "USB 2 (High-Speed)")


def _security(driver: "DSLogicDriver") -> SelfTestResult:
    if driver.security_passed:
        return SelfTestResult("SECURITY", "OK", "The FPGA accepted the key of the board")
    return SelfTestResult("SECURITY", "FAIL", "The FPGA did not accept the key: it captures nothing")


def _buffer_capture(driver: "DSLogicDriver") -> SelfTestResult:
    channels = min(COUNTER_CHANNELS, driver.channel_count)
    session = _capture(driver, TEST_RATE, BUFFER_SAMPLES, channels)
    depth = driver.profile.hw_depth >> 20
    used = len(session.capture_channels[0].samples) * channels >> 20
    return _counter_result(
        "BUFFER_CAPTURE", session, f"on {channels} channels through {used} of {depth} Mbit memory", channels
    )


def _stream_capture(driver: "DSLogicDriver") -> SelfTestResult:
    channels = min(COUNTER_CHANNELS, driver.channel_count)
    rate = min(
        protocol.max_rate(driver.profile, driver.info.super_speed, range(channels), AcquisitionMode.STREAM),
        TEST_RATE,
    )
    started = time.monotonic()
    session = _capture(driver, rate, int(rate * STREAM_SECONDS), channels, AcquisitionMode.STREAM)
    elapsed = time.monotonic() - started
    megabytes = len(session.capture_channels[0].samples) * channels / 8 / 1e6
    return _counter_result(
        "STREAM_CAPTURE",
        session,
        f"on {channels} channels at {rate / 1e6:g} MHz ({megabytes / elapsed:.0f} MB/s)",
        channels,
    )


def _trigger_position(session: CaptureSession) -> tuple[np.ndarray, int]:
    word = _counter(session)
    position = session.pre_trigger_samples
    if not 0 < position < len(word):
        raise _CaptureFailed(f"Trigger position {position} outside of the capture")
    return word, position


def _pattern_trigger(driver: "DSLogicDriver") -> SelfTestResult:
    session = _capture(
        driver, TEST_RATE, 20_000, COUNTER_CHANNELS, pre_trigger_samples=1_000,
        trigger_type=TriggerType.COMPLEX, trigger_channel=0, trigger_bit_count=8,
        trigger_pattern=TRIGGER_PATTERN,
    )
    word, position = _trigger_position(session)
    found = int(word[position]) & 0xFF
    if found != TRIGGER_PATTERN:
        return SelfTestResult(
            "PATTERN_TRIGGER", "FAIL", f"At the trigger channels 1-8 read 0x{found:02X} instead of 0x{TRIGGER_PATTERN:02X}"
        )
    return SelfTestResult("PATTERN_TRIGGER", "OK", f"Pattern 0x{TRIGGER_PATTERN:02X} on channels 1-8 at sample {position}")


def _edge_trigger(driver: "DSLogicDriver") -> SelfTestResult:
    session = _capture(
        driver, TEST_RATE, 20_000, COUNTER_CHANNELS, pre_trigger_samples=1_000,
        trigger_type=TriggerType.EDGE, trigger_channel=TRIGGER_EDGE_CHANNEL, trigger_inverted=False,
    )
    word, position = _trigger_position(session)
    before, at = (int(word[index]) >> TRIGGER_EDGE_CHANNEL & 1 for index in (position - 1, position))
    channel = TRIGGER_EDGE_CHANNEL + 1
    if (before, at) != (0, 1):
        return SelfTestResult("EDGE_TRIGGER", "FAIL", f"No rising edge of channel {channel} at the trigger")
    return SelfTestResult("EDGE_TRIGGER", "OK", f"Rising edge of channel {channel} at sample {position}")


def _inputs(driver: "DSLogicDriver") -> list[SelfTestResult]:
    try:
        session = _capture(
            driver, INPUT_RATE, INPUT_SAMPLES, driver.channel_count, internal_test=False
        )
    except _CaptureFailed as error:
        return [SelfTestResult("INPUTS", "FAIL", str(error))]
    results = []
    for channel in session.capture_channels:
        samples = channel.samples
        edges = int(np.count_nonzero(np.diff(samples)))
        item = f"CH{channel.channel_number + 1}"
        if edges:
            results.append(SelfTestResult(item, "ACTIVE", f"{edges:,} edges: a signal is connected"))
        elif samples[0]:
            results.append(SelfTestResult(item, "STUCK_HIGH"))
        else:
            results.append(SelfTestResult(item, "OK", "Reads low"))
    return results
