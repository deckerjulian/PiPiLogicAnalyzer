"""Driver logic tests, exercised through a fake transport."""

from __future__ import annotations

import struct
import threading
import time

import numpy as np
import pytest

from openscilab.driver.pico import protocol
from openscilab.driver.pico.analyzer import PicoDriver, unpack_channel_samples
from openscilab.driver.base import (
    FirmwareOutdatedError,
    AnalyzerDriverType,
    CaptureError,
    CaptureMode,
)
from openscilab.driver.pico.protocol import parse_version
from openscilab.driver.emulated import EmulatedAnalyzerDriver
from openscilab.driver.models import AnalyzerChannel, BurstInfo, CaptureSession, TriggerType
from openscilab.driver.base import DeviceConnectionError as ConnectionRefused
from openscilab.driver.pico.transport import Transport, TransportError


class FakeTransport(Transport):
    """Serves canned responses and records everything written to it."""

    def __init__(self, channels: int = 24, buffer_size: int = 131072) -> None:
        self.written = bytearray()
        self.closed = False
        self.reopened = 0
        self._aborted = threading.Event()
        self._responses: list[str] = [
            "LOGIC_ANALYZER_V6_0",
            "FREQ:100000000",
            "BLASTFREQ:200000000",
            f"BUFFER:{buffer_size}",
            f"CHANNELS:{channels}",
            f"PROTOCOL:{protocol.FIRMWARE_PROTOCOL}",
        ]
        self._data = bytearray()

    # -- helpers used by the tests
    def queue_response(self, line: str) -> None:
        self._responses.append(line)

    def queue_data(self, payload: bytes) -> None:
        self._data.extend(payload)

    # -- Transport interface
    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def read_line(self, timeout=None) -> str:
        if not self._responses:
            raise TransportError("no response queued")
        return self._responses.pop(0)

    def read_exactly(self, count: int, timeout=None) -> bytes:
        # A real device keeps the reader waiting until the bytes arrive, so
        # block here too instead of failing immediately.
        deadline = time.monotonic() + 5
        while len(self._data) < count:
            if self._aborted.is_set():
                raise TransportError("read aborted")
            if time.monotonic() > deadline:
                raise TransportError("not enough data queued")
            time.sleep(0.005)
        data = bytes(self._data[:count])
        del self._data[:count]
        return data

    def fail_pending_reads(self) -> None:
        """Make a blocked reader fail, as a disconnected device would."""
        self._aborted.set()

    def reset_input(self) -> None:
        # The real transports drop stale bytes here; the queued test payload
        # represents data that arrives *after* the capture request.
        pass

    def reopen(self) -> None:
        self.reopened += 1
        self._aborted.set()

    def close(self) -> None:
        self.closed = True
        self._aborted.set()

    @property
    def is_open(self) -> bool:
        return not self.closed


@pytest.fixture
def driver(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(
        "openscilab.driver.pico.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    instance = PicoDriver("/dev/fake")
    instance.test_transport = transport  # type: ignore[attr-defined]
    return instance


def make_session(channels: int = 4, pre: int = 4, post: int = 12) -> CaptureSession:
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=pre, post_trigger_samples=post)
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in range(channels)]
    return session


# ------------------------------------------------------------------- identity
def test_version_parsing():
    version = parse_version("PIPI_LOGIC_ANALYZER_PICO_V7_1")
    assert (version.major, version.minor) == (7, 1)
    assert parse_version("garbage").major == 0


def open_with(monkeypatch, transport: FakeTransport, **options) -> PicoDriver:
    monkeypatch.setattr(
        "openscilab.driver.pico.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    return PicoDriver("/dev/fake", **options)


@pytest.mark.parametrize("protocol_line", [None, "PROTOCOL:0", f"PROTOCOL:{protocol.FIRMWARE_PROTOCOL + 1}"])
def test_only_the_current_firmware_is_opened(monkeypatch, protocol_line):
    transport = FakeTransport()
    transport._responses.pop()
    if protocol_line:
        transport._responses.append(protocol_line)
    with pytest.raises(FirmwareOutdatedError) as raised:
        open_with(monkeypatch, transport)
    assert raised.value.location == "/dev/fake" and "Install the firmware" in str(raised.value)
    assert transport.closed

    # Opened anyway to read the identification or to restart it for the update
    transport = FakeTransport()
    transport._responses.pop()
    board = open_with(monkeypatch, transport, require_current_firmware=False)
    assert board.protocol_version is None and board.device_version == "LOGIC_ANALYZER_V6_0"


def test_the_current_firmware_reports_its_protocol(driver):
    assert driver.protocol_version == protocol.FIRMWARE_PROTOCOL


def test_driver_reads_the_device_identity(driver):
    assert driver.device_version == "LOGIC_ANALYZER_V6_0"
    assert driver.max_frequency == 100_000_000
    assert driver.blast_frequency == 200_000_000
    assert driver.buffer_size == 131072
    assert driver.channel_count == 24
    assert driver.driver_type is AnalyzerDriverType.SERIAL
    assert driver.test_transport.written.startswith(protocol.command_packet(protocol.CMD_GET_ID))


def test_min_frequency_follows_the_maximum(driver):
    assert driver.min_frequency == (100_000_000 * 2) // 65535


# --------------------------------------------------------------------- limits
@pytest.mark.parametrize(
    "channels,mode",
    [([0, 1], CaptureMode.CHANNELS_8), ([0, 9], CaptureMode.CHANNELS_16), ([20], CaptureMode.CHANNELS_24)],
)
def test_capture_mode_depends_on_the_highest_channel(driver, channels, mode):
    assert driver.get_capture_mode(channels) is mode


def test_limits_scale_with_the_capture_mode(driver):
    eight = driver.get_limits([0])
    twenty_four = driver.get_limits([20])
    assert eight.max_post_samples == 131072 - 2
    assert twenty_four.max_post_samples == 131072 // 4 - 2
    assert eight.max_total_samples == eight.min_pre_samples + eight.max_post_samples


# ----------------------------------------------------------------- validation
def test_validate_rejects_out_of_range_settings(driver):
    session = make_session()
    assert driver.validate_settings(session, session.total_samples)

    session.frequency = driver.max_frequency * 2
    assert not driver.validate_settings(session, session.total_samples)


def test_blast_requires_the_blast_frequency_and_no_pre_samples(driver):
    session = make_session(pre=0, post=1000)
    session.trigger_type = TriggerType.BLAST
    session.frequency = driver.blast_frequency
    assert driver.validate_settings(session, session.total_samples)

    session.pre_trigger_samples = 10
    assert not driver.validate_settings(session, session.total_samples)


def test_pattern_trigger_bit_count_is_bounded(driver):
    driver.test_transport.queue_response("CAPS:SELFTEST,PATTERN_GROUPS=0-20/21-23")
    session = make_session()
    session.trigger_type = TriggerType.FAST
    session.trigger_channel = 0
    session.trigger_bit_count = 5
    assert driver.validate_settings(session, session.total_samples)

    session.trigger_bit_count = 6  # fast triggers only cover five channels
    assert not driver.validate_settings(session, session.total_samples)


def test_pattern_trigger_follows_the_reported_groups(driver):
    session = make_session()
    session.trigger_type = TriggerType.COMPLEX
    session.trigger_channel = 16
    session.trigger_bit_count = 4

    # A board without pattern triggers reports no groups
    assert driver.pattern_trigger_groups() == ()
    assert not driver.validate_settings(session, session.total_samples)

    driver.test_transport.queue_response("CAPS:SELFTEST,EDGE_TRIGGER_OUT,PATTERN_GROUPS=0-20/21-23")
    driver.refresh_capabilities()
    assert driver.pattern_trigger_groups() == ((0, 21), (21, 3))
    assert driver.validate_settings(session, session.total_samples)  # channels 17 to 20

    session.trigger_channel = 19  # channels 20 to 23 are not consecutive GPIOs
    assert not driver.validate_settings(session, session.total_samples)

    session.trigger_type = TriggerType.FAST
    session.trigger_channel = 16
    session.trigger_bit_count = 5
    assert driver.validate_settings(session, session.total_samples)


def test_edge_trigger_with_output_needs_the_capability(driver):
    session = make_session()
    session.frequency = driver.max_frequency
    session.trigger_type = TriggerType.EDGE_OUT
    session.trigger_channel = 3
    session.trigger_inverted = True
    assert not driver.validate_settings(session, session.total_samples)

    driver.test_transport.queue_response("CAPS:EDGE_TRIGGER_OUT")
    assert not driver.validate_settings(session, session.total_samples)  # a failed query is not repeated at once
    driver.refresh_capabilities()
    assert driver.validate_settings(session, session.total_samples)

    request = driver.compose_request(session, CaptureMode.CHANNELS_8)
    offset = driver.trigger_offset(session)
    assert offset > 0
    assert (request.trigger_type, request.trigger, request.inverted_or_count) == (5, 3, 1)
    assert (request.pre_samples, request.post_samples) == (4 + offset, 12 - offset)


# -------------------------------------------------------------------- capture
def build_capture_payload(samples: list[int], mode: CaptureMode, timestamps: list[int] = ()) -> bytes:
    dtype = {1: "<u1", 2: "<u2", 4: "<u4"}[mode.bytes_per_sample]
    payload = struct.pack("<I", len(samples))
    payload += np.asarray(samples, dtype=dtype).tobytes()
    payload += bytes([len(timestamps)])
    if len(timestamps) > 1:
        payload += np.asarray(timestamps, dtype="<u4").tobytes()
    return payload


def test_start_capture_sends_the_request_and_decodes_the_samples(driver):
    session = make_session(channels=4, pre=2, post=6)
    transport = driver.test_transport
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(build_capture_payload([0b0001, 0b0011, 0b0000, 0b1111] * 2, CaptureMode.CHANNELS_8))

    done = threading.Event()
    results = []

    assert driver.start_capture(session, lambda args: (results.append(args), done.set())) is CaptureError.NONE
    assert done.wait(5)

    args = results[0]
    assert args.success
    assert np.array_equal(session.capture_channels[0].samples, [1, 1, 0, 1, 1, 1, 0, 1])
    assert np.array_equal(session.capture_channels[1].samples, [0, 1, 0, 1, 0, 1, 0, 1])
    assert np.array_equal(session.capture_channels[3].samples, [0, 0, 0, 1, 0, 0, 0, 1])
    assert not driver.is_capturing


def test_capture_is_rejected_when_the_device_reports_an_error(driver):
    driver.test_transport.queue_response("CAPTURE_ERROR")
    assert driver.start_capture(make_session()) is CaptureError.HARDWARE_ERROR


def test_capture_rejects_bad_parameters(driver):
    session = make_session()
    session.pre_trigger_samples = -5
    assert driver.start_capture(session) is CaptureError.BAD_PARAMS


def test_second_capture_is_refused_while_one_is_running(driver, monkeypatch):
    session = make_session()
    driver.test_transport.queue_response("CAPTURE_STARTED")
    monkeypatch.setattr(driver, "_read_capture", lambda *args: None)
    assert driver.start_capture(session) is CaptureError.NONE
    assert driver.start_capture(session) is CaptureError.BUSY


def test_timestamp_block_is_only_read_when_the_device_sends_one(driver):
    """The firmware transmits timestamps only when it reports more than one."""
    # Burst measurement needs at least 100 post-trigger samples (V6_5).
    session = make_session(channels=1, pre=2, post=100)
    session.loop_count = 2
    session.measure_bursts = True

    transport = driver.test_transport
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(build_capture_payload([1, 0, 1, 0], CaptureMode.CHANNELS_8, timestamps=[1]))

    done = threading.Event()
    results = []
    driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert done.wait(5)
    assert results[0].success
    assert session.bursts is None


def test_unpack_channel_samples():
    raw = np.array([0b101, 0b010, 0b111], dtype=np.uint32)
    assert np.array_equal(unpack_channel_samples(raw, 0), [1, 0, 1])
    assert np.array_equal(unpack_channel_samples(raw, 1), [0, 1, 1])
    assert np.array_equal(unpack_channel_samples(raw, 2), [1, 0, 1])


def test_compose_request_compensates_the_pattern_trigger_delay(driver):
    session = make_session(pre=100, post=200)
    session.trigger_type = TriggerType.COMPLEX
    session.trigger_bit_count = 2
    request = driver.compose_request(session, CaptureMode.CHANNELS_8)
    offset = driver.trigger_offset(session)

    assert offset >= 0
    assert request.pre_samples == 100 + offset
    assert request.post_samples == 200 - offset


def test_edge_request_keeps_the_sample_counts(driver):
    session = make_session(pre=100, post=200)
    request = driver.compose_request(session, CaptureMode.CHANNELS_8)
    assert (request.pre_samples, request.post_samples) == (100, 200)


def test_stop_capture_reconnects_the_transport(driver, monkeypatch):
    driver.test_transport.queue_response("CAPTURE_STARTED")
    monkeypatch.setattr(driver, "_read_capture", lambda *args: None)
    driver.start_capture(make_session())

    assert driver.stop_capture()
    assert driver.test_transport.reopened == 1
    assert not driver.is_capturing


# ------------------------------------------------------------------ emulated
def test_emulated_driver_reports_limits_per_device():
    emulated = EmulatedAnalyzerDriver(2)
    assert emulated.channel_count == 48
    assert emulated.blast_frequency == 0  # must not raise
    limits = emulated.get_limits([0, 30])
    assert limits.max_post_samples > 0


def test_burst_info_formats_the_gap():
    assert BurstInfo(burst_time_gap=500).get_time() == "500 ns"
    assert BurstInfo(burst_time_gap=1500).get_time() == "1.500 µs"
    assert BurstInfo(burst_time_gap=1_500_000).get_time() == "1.500 ms"
    assert BurstInfo(burst_time_gap=1_500_000_000).get_time() == "1.500 s"


def test_session_clone_is_deep():
    session = make_session()
    session.capture_channels[0].samples = np.array([1, 0, 1], dtype=np.uint8)
    clone = session.clone()
    clone.capture_channels[0].samples[0] = 0
    assert session.capture_channels[0].samples[0] == 1

    settings_only = session.clone_settings()
    assert settings_only.capture_channels[0].samples is None


def test_a_failed_read_is_reported_to_the_handler(driver):
    """A truncated response must surface as a failed capture, not silence."""
    session = make_session(channels=1, pre=2, post=6)
    transport = driver.test_transport
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    transport.queue_data(struct.pack("<I", 8))  # length only, no samples

    done = threading.Event()
    results = []
    driver.start_capture(session, lambda args: (results.append(args), done.set()))
    transport.fail_pending_reads()

    assert done.wait(5)
    assert not results[0].success
    assert results[0].error
    assert not driver.is_capturing


def test_aborting_a_capture_does_not_report_an_error(driver):
    session = make_session(channels=1, pre=2, post=6)
    transport = driver.test_transport
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")

    results = []
    driver.start_capture(session, results.append)
    driver.stop_capture()

    # The reader thread fails on the missing payload, which is expected here.
    if driver._capture_thread is not None:
        driver._capture_thread.join(5)
    assert results == []


def test_the_interceptor_mode_follows_the_sample_bits(driver):
    # Standard boards: the channel number is the bit in the samples.
    assert driver.get_capture_mode([0, 7]) is CaptureMode.CHANNELS_8

    # Interceptor: channel 0 is GPIO 6 = bit 4, channel 4 is bit 8, channel 12 is bit 16.
    driver._version = "PIPI_LOGIC_ANALYZER_INTERCEPTOR_V7_1"
    assert driver.get_capture_mode([0, 3]) is CaptureMode.CHANNELS_8
    assert driver.get_capture_mode([4]) is CaptureMode.CHANNELS_16
    assert driver.get_capture_mode([12]) is CaptureMode.CHANNELS_24


# ---------------------------------------------------------------- reliability
def test_a_board_left_streaming_is_resynchronised_on_open(monkeypatch):
    """A board still sending for a program that is gone answers the identification with
    leftovers first: the driver stops it and asks once more."""
    transport = FakeTransport()
    transport._responses.insert(0, "\x00\x17garbage of a stream")
    resets = []
    monkeypatch.setattr(transport, "reset_input", lambda: resets.append(len(transport.written)))
    monkeypatch.setattr("openscilab.driver.pico.analyzer.RESYNC_DELAY_S", 0.0)
    opened = open_with(monkeypatch, transport)
    assert opened.device_version == "LOGIC_ANALYZER_V6_0"
    assert protocol.CMD_ABORT_CAPTURE in bytes(transport.written)
    assert len(resets) >= 2  # before the first question and after stopping the board


def test_a_board_that_does_not_identify_is_refused(monkeypatch):
    transport = FakeTransport()
    transport._responses[:0] = ["nonsense", "more nonsense"]
    monkeypatch.setattr("openscilab.driver.pico.analyzer.RESYNC_DELAY_S", 0.0)
    with pytest.raises(ConnectionRefused, match="Invalid device identification"):
        open_with(monkeypatch, transport)
    assert transport.closed


def test_a_stopped_capture_does_not_end_the_next_one(driver):
    """Stop and start again at once: the reader of the stopped capture must not report a
    failure or mark the device idle while the new capture runs."""
    transport = driver.test_transport
    results = []
    gates = [threading.Event(), threading.Event()]
    readers = []

    def read_exactly(count, timeout=None):
        gate = gates[len(readers)]
        readers.append(threading.current_thread())
        gate.wait(5)
        raise TransportError("the port was closed")

    transport.read_exactly = read_exactly
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    driver.start_capture(make_session(channels=1, pre=2, post=6), results.append)
    old_thread = driver._capture_thread
    while not readers:
        time.sleep(0.001)
    driver.stop_capture()  # the old reader is still blocked in its read
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    assert driver.start_capture(make_session(channels=1, pre=2, post=6), results.append) == CaptureError.NONE
    gates[0].set()  # only now the old reader wakes up and fails
    old_thread.join(5)
    assert driver.is_capturing and results == []  # it stayed silent and left the new capture alone
    driver.stop_capture()
    gates[1].set()


def test_a_failed_capability_query_is_not_repeated_for_every_property(driver):
    transport = driver.test_transport
    asked = []
    original = transport.read_line

    def read_line(timeout=None):
        asked.append(timeout)
        return original(timeout)

    transport.read_line = read_line
    for _ in range(5):
        assert driver.capabilities() == frozenset()
        driver.supports_state_mode()
    assert len(asked) == 1  # one question, not one per property
    transport.queue_response("CAPS:STATE_MODE")
    assert "STATE_MODE" in driver.refresh_capabilities()


def test_device_details_are_read_once(driver):
    transport = driver.test_transport
    transport.queue_response("CAPS:DEVICEINFO")
    for line in ("INFO:chip:RP2040", "INFO_END"):
        transport.queue_response(line)
    assert driver.device_details() == {"chip": "RP2040"}
    written = len(transport.written)
    assert driver.device_details() == {"chip": "RP2040"} and len(transport.written) == written
