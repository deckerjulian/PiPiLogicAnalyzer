"""Large captures: min/max overviews, tiles, the visible range first, resuming, waiting for it."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core.overview import Overview, ProgressiveCapture, column_envelope
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated


def wait_for(condition, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# ------------------------------------------------------------------ overview
def test_the_overview_envelope_contains_the_exact_one():
    values = np.sin(np.arange(200_000) / 300.0) + np.random.default_rng(1).normal(0, 0.01, 200_000)
    overview = Overview.build(values)
    for first, last, columns in ((0, 200_000, 400), (1234, 98_765, 333), (50_000, 50_400, 100)):
        low, high = overview.envelope(first, last, columns)
        exact_low, exact_high = column_envelope(values[first:last], columns)
        assert np.all(low <= exact_low + 1e-12) and np.all(high >= exact_high - 1e-12)
        assert np.max(exact_low - low) < 0.1  # not much wider


def test_an_overview_from_blocks():
    mins = np.array([0, 0, 1, 0])
    maxs = np.array([1, 0, 1, 1])
    overview = Overview.from_blocks(mins, maxs, 4096, base=1024)
    assert overview.block(0) == 1024 and overview.block(1) == 16384
    low, high = overview.envelope(0, 4096, 4)
    assert list(low) == [0, 0, 1, 0] and list(high) == [1, 0, 1, 1]


def test_tiles_priority_and_ranges():
    progressive = ProgressiveCapture(1000, tile_samples=100)
    assert progressive.tile_count == 10 and not progressive.complete
    progressive.prioritize(550, 720)
    order = []
    while (tile := progressive.next_tile()) is not None:
        order.append(tile)
        progressive.mark_loaded(tile)
        if len(order) == 3:
            assert progressive.is_loaded(550, 720)
    assert order[:3] == [5, 6, 7] and sorted(order) == list(range(10))
    assert progressive.complete and progressive.loaded_ranges() == [(0, 1000)]


# ----------------------------------------------------------------- simulator
def big_capture(samples: int = 3_000_000, analog: int = 2):
    instrument = open_simulated("dho924s", fast=True)
    driver = instrument.capture.driver
    session = CaptureSession(frequency=100_000_000, pre_trigger_samples=0, post_trigger_samples=samples,
                             trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in (0, 15)]
    session.analog_channels = [AnalogChannel(channel_number=index) for index in range(analog)]
    return instrument, driver, session


def test_a_large_capture_is_complete_at_once_as_an_overview():
    instrument, driver, session = big_capture()
    tiles = []
    finished = threading.Event()
    driver.add_capture_tile_handler(lambda args: (tiles.append(args.first), args.complete and finished.set()))
    completed = threading.Event()
    started = time.monotonic()
    assert driver.start_capture(session, lambda args: completed.set()).name == "NONE"
    assert completed.wait(10)
    assert time.monotonic() - started < 2  # long before 3 million samples × 4 channels arrived
    progressive = session.progressive
    assert progressive is not None and progressive.overviews.keys() == {("d", 0), ("d", 15), ("a", 0), ("a", 1)}
    low, high = progressive.overviews[("a", 0)].envelope(0, session.post_trigger_samples, 10)
    assert (low * session.analog_channels[0].scale).max() < -1.8  # the 2 V sine, already visible
    assert finished.wait(30)
    assert progressive.complete and len(tiles) == progressive.tile_count
    # the tiles hold the same samples as an ordinary capture of the same time
    expected = driver.sample_analog(0, driver.last_trigger_time, 100e6, 1000)
    assert np.array_equal(session.analog_channels[0].raw[:1000], expected)


def test_the_display_asks_for_the_visible_range_first(shell, make_dataview):
    instrument, driver, session = big_capture(4_000_000, analog=1)
    shell.hub.add(instrument)
    window = make_dataview()
    window.use_instrument(instrument)
    driver.fast = False  # paced by the network rate of the profile
    driver.profile["progressive"]["lan_rate"] = 20_000_000
    window._begin_capture(session)
    assert wait_for(lambda: window.model.session is session and window.model.progressive is not None, 10)
    window.model.set_view(3_000_000, 200_000)
    window._prioritize_visible()
    progressive = session.progressive
    assert wait_for(lambda: progressive.is_loaded(3_000_000, 3_200_000), 10)
    assert not progressive.complete  # the visible part came before the rest
    assert not window.sample_viewer.grab().isNull() and not window.analog_viewer.grab().isNull()
    assert "Transferring" in window.statusBar().currentMessage()

    # saving waits until all of it arrived
    driver.profile["progressive"]["lan_rate"] = 0
    assert window.wait_for_transfer("Save capture")
    assert window.model.progressive is None
    assert wait_for(lambda: window.model.transitions_for(session.capture_channels[0]) is not None, 10)


def test_an_interrupted_transfer_resumes(shell, make_dataview):
    instrument, driver, session = big_capture(2_000_000, analog=0)
    shell.hub.add(instrument)
    window = make_dataview()
    window.use_instrument(instrument)
    driver.fast = False
    driver.profile["progressive"]["lan_rate"] = 5_000_000
    window._begin_capture(session)
    assert wait_for(lambda: session.progressive is not None, 10)
    driver.stop_transfer()
    assert wait_for(lambda: session.progressive.interrupted, 10)
    assert wait_for(lambda: not window.transfer_banner.isHidden(), 5)
    driver.profile["progressive"]["lan_rate"] = 0
    assert window.resume_transfer()
    assert wait_for(lambda: session.progressive.complete, 30)
    assert wait_for(lambda: window.transfer_banner.isHidden(), 5)


def test_decoders_wait_for_the_transfer(make_dataview):
    window = make_dataview()
    manager = window.decoder_manager
    session = CaptureSession(frequency=1000, pre_trigger_samples=0, post_trigger_samples=4096)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.zeros(4096, np.uint8))]
    session.progressive = ProgressiveCapture(4096, tile_samples=1024)
    window.load_session(session)
    manager.provider.instances.append(object())  # any decoder
    try:
        manager.decode()
        assert manager._waiting_for_transfer and "transfer" in manager.status_label.text()
    finally:
        manager.provider.instances.clear()


def test_export_to_pulseview_has_the_analog_traces(tmp_path):
    from openscilab.core import sigrok_session

    instrument, driver, session = big_capture(1_200_000, analog=4)
    completed = threading.Event()
    finished = threading.Event()
    driver.add_capture_tile_handler(lambda args: args.complete and finished.set())
    driver.start_capture(session, lambda args: completed.set())
    assert completed.wait(10) and finished.wait(30)
    path = tmp_path / "dho.sr"
    sigrok_session.save_session(str(path), session)
    loaded = sigrok_session.load_session(str(path))
    assert [channel.channel_name for channel in loaded.analog_channels] == ["CH1", "CH2", "CH3", "CH4"]
    assert loaded.analog_channels[3].volts().max() == pytest.approx(3.3, abs=0.05)
