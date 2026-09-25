"""Stream captures of the PiPiLogicAnalyzer firmware (trigger type 6), against a fake transport."""

from __future__ import annotations

import os
import struct
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from pipilogicanalyzer.driver import analyzer, protocol
from pipilogicanalyzer.driver.analyzer import PiPiLogicAnalyzerDriver
from pipilogicanalyzer.driver.base import (
    ACQUISITION_STREAM,
    CAPABILITY_CONTINUOUS_STREAM,
    CAPABILITY_STREAM_IMMEDIATE_ONLY,
    CaptureError,
)
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType

from test_driver import FakeTransport


class StreamingTransport(FakeTransport):
    """Ends the stream with its marker when the host sends the stop byte, as the firmware does."""

    def __init__(self) -> None:
        super().__init__()
        self.stops = 0

    def write(self, data: bytes) -> None:
        super().write(data)
        if bytes(data) == bytes([protocol.CMD_ABORT_CAPTURE]):
            self.stops += 1
            if self.stops == 1:
                self.queue_data(struct.pack("<I", analyzer.STREAM_END_STOPPED))


@pytest.fixture
def pico(monkeypatch):
    transport = StreamingTransport()
    transport.queue_response("CAPS:SELFTEST,DEVICEINFO,STREAM=800000")
    monkeypatch.setattr("pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport)
    monkeypatch.setattr(analyzer, "STREAM_PROGRESS_INTERVAL", 0)
    driver = PiPiLogicAnalyzerDriver("/dev/fake")
    driver.capabilities()
    driver.test_transport = transport  # type: ignore[attr-defined]
    return driver


def stream_session(channels=(0, 1, 2), samples=1000, rate=100_000, continuous=False) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=0, post_trigger_samples=samples)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    session.acquisition_mode = ACQUISITION_STREAM
    session.trigger_type = TriggerType.IMMEDIATE
    session.continuous = continuous
    return session


def chunks(words: np.ndarray, size: int = 300) -> bytes:
    data = words.astype("<u1").tobytes()
    out = bytearray()
    for start in range(0, len(data), size):
        piece = data[start:start + size]
        out += struct.pack("<I", len(piece)) + piece
    return bytes(out)


def run(driver, session, stop=None):
    done = threading.Event()
    results = []
    progress = []
    driver.add_capture_progress_handler(lambda args: progress.append((args.first_sample, args.sample_count)))
    error = driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert error is CaptureError.NONE
    if stop is not None:
        stop()
    assert done.wait(5), "the stream did not end"
    return results[0], progress


def test_the_pico_offers_a_stream_limited_by_usb(pico):
    assert pico.acquisition_modes() == ("buffer", "stream")
    assert {CAPABILITY_CONTINUOUS_STREAM, CAPABILITY_STREAM_IMMEDIATE_ONLY} <= pico.capabilities()
    assert pico.max_frequency_for(range(8), ACQUISITION_STREAM) == 800_000
    assert pico.max_frequency_for(range(16), ACQUISITION_STREAM) == 400_000
    assert pico.max_frequency_for(range(24), ACQUISITION_STREAM) == 200_000
    assert pico.max_frequency_for(range(8), "buffer") == pico.max_frequency
    assert pico.get_limits(range(4), ACQUISITION_STREAM).max_post_samples == (1 << 30) // 4


def test_a_stream_of_fixed_length_stops_itself(pico):
    transport = pico.test_transport
    words = np.arange(3000) & 0xFF
    transport.queue_response("STREAM_STARTED:0,1,5")  # channel 2 is on bit 5
    transport.queue_data(chunks(words))
    result, progress = run(pico, stream_session(samples=1000))
    assert result.success and result.error is None
    request = protocol.CaptureRequest(trigger_type=6, channels=[0, 1, 2], channel_count=3, frequency=100_000)
    assert protocol.command_packet(protocol.CMD_START_CAPTURE, request.pack(pico._request_layout)) in transport.written
    assert transport.stops == 1  # stopped once 1000 samples arrived
    channels = result.session.capture_channels
    assert len(channels[0].samples) == 1000 and result.session.post_trigger_samples == 1000
    assert np.array_equal(channels[0].samples, words[:1000] & 1)
    assert np.array_equal(channels[2].samples, (words[:1000] >> 5) & 1)
    assert progress and all(first == 0 for first, _count in progress)


def test_an_overflow_keeps_the_samples_before_the_last_chunk(pico):
    transport = pico.test_transport
    words = np.arange(900) & 0xFF
    transport.queue_response("STREAM_STARTED:0,1,2")
    transport.queue_data(chunks(words) + struct.pack("<I", analyzer.STREAM_END_OVERFLOW))
    result, _progress = run(pico, stream_session(samples=100_000))
    assert result.success and "could not keep up" in result.error
    assert len(result.session.capture_channels[0].samples) == 600  # the last 300 are dropped


def test_an_endless_stream_keeps_its_end_until_stopped(pico):
    transport = pico.test_transport
    words = np.arange(5000) & 0xFF
    transport.queue_response("STREAM_STARTED:0,1,2")
    transport.queue_data(chunks(words))
    session = stream_session(samples=700, continuous=True)
    arrived = threading.Event()
    pico.add_capture_progress_handler(lambda args: args.first_sample + args.sample_count >= 4700 and arrived.set())

    def stop():
        assert arrived.wait(5)
        assert pico.stop_capture()

    result, progress = run(pico, session, stop)
    assert result.success
    samples = result.session.capture_channels[1].samples
    assert len(samples) == 700 and np.array_equal(samples, (words[-700:] >> 1) & 1)
    assert progress[-1][0] > 0  # the window moved
    assert transport.reopened == 0  # stopped with the end marker, no reconnect


def test_invalid_streams_are_rejected(pico):
    assert pico.start_capture(stream_session(rate=900_000)) is CaptureError.BAD_PARAMS
    edge = stream_session()
    edge.trigger_type = TriggerType.EDGE
    assert pico.start_capture(edge) is CaptureError.BAD_PARAMS
    assert pico.start_capture(stream_session(samples=(1 << 30) // 3 + 1)) is CaptureError.BAD_PARAMS


def test_firmware_without_the_stream_offers_the_buffer_only(monkeypatch):
    transport = FakeTransport()
    transport.queue_response("CAPS:SELFTEST,DEVICEINFO")
    monkeypatch.setattr("pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport)
    driver = PiPiLogicAnalyzerDriver("/dev/fake")
    assert driver.acquisition_modes() == ()
    assert driver.start_capture(stream_session()) is CaptureError.BAD_PARAMS


def test_the_dialog_starts_a_pico_stream_at_once(pico):
    from PySide6.QtWidgets import QApplication

    from pipilogicanalyzer.ui.dialogs.capture_dialog import CaptureDialog

    QApplication.instance() or QApplication([])
    dialog = CaptureDialog(pico)
    try:
        assert dialog.immediate_radio.isHidden() and dialog.edge_radio.isEnabled()
        dialog.acquisition_box.setCurrentIndex(dialog.acquisition_box.findData(ACQUISITION_STREAM))
        assert not dialog.immediate_radio.isHidden() and dialog.immediate_radio.isChecked()
        assert not dialog.edge_radio.isEnabled() and not dialog.pattern_radio.isEnabled()
        assert not dialog.continuous_box.isHidden()
        for selector in dialog.channel_selectors:
            selector.enabled = selector.channel_number < 16
        dialog._update_limits()
        assert dialog.frequency_box.maximum() == 400_000
        dialog.continuous_box.setChecked(True)
        dialog.post_samples_box.setValue(50_000)
        dialog._accept()
        session = dialog.selected_settings
        assert session.trigger_type == TriggerType.IMMEDIATE and session.continuous
        assert session.frequency <= 400_000

        dialog.acquisition_box.setCurrentIndex(dialog.acquisition_box.findData("buffer"))
        assert dialog.edge_radio.isEnabled() and dialog.edge_radio.isChecked()
        assert dialog.immediate_radio.isHidden()
    finally:
        dialog.close()


@pytest.mark.parametrize("broken", [False, True])
def test_the_self_test_streams_the_test_counter(pico, broken):
    transport = pico.test_transport
    samples = 400_000  # half a second at 800 kHz
    words = (-np.arange(samples + 5000)) & 0xFF
    if broken:
        words[1234] ^= 0x10
    transport.queue_response("SELFTEST:RAM:OK:393216 bytes")
    transport.queue_response("SELFTEST_END")
    transport.queue_response("STREAM_STARTED:0,1,2,3,4,5,6,7")
    transport.queue_data(chunks(words, 4096))
    results = {result.item: result for result in pico.run_self_test()}
    request = protocol.CaptureRequest(trigger_type=6, trigger_value=1, channels=list(range(8)), channel_count=8,
                                      frequency=800_000)
    assert protocol.command_packet(protocol.CMD_START_CAPTURE, request.pack(pico._request_layout)) in transport.written
    assert results["RAM"].severity == "ok"
    if broken:
        assert results["STREAM"].severity == "fail" and "2 of 400,000" in results["STREAM"].detail
    else:
        assert results["STREAM"].severity == "ok" and "800 kHz" in results["STREAM"].detail
