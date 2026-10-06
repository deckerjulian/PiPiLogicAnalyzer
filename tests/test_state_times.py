"""State captures with times: simulator, view per state / real time, cursors, files, live state,
software triggers on states."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import capture_io
from openscilab.core.state_mode import states_to_timing
from openscilab.driver.models import AnalyzerChannel, CaptureSession, EdgeKind, TriggerType
from openscilab.driver.simulated import open_simulated
from openscilab.ui.widgets import time_axis


def state_capture(count: int = 50, stream: bool = False) -> tuple[object, CaptureSession]:
    instrument = open_simulated("free", fast=True)
    session = CaptureSession(frequency=100_000_000, pre_trigger_samples=0, post_trigger_samples=count,
                             trigger_type=TriggerType.IMMEDIATE, clock_channel=8, clock_edge=EdgeKind.RISING,
                             acquisition_mode="stream" if stream else "buffer")
    session.capture_channels = [AnalyzerChannel(channel_number=bit, channel_name=f"D{bit}") for bit in range(4)]
    return instrument, session


def counter(session: CaptureSession) -> list[int]:
    return [int(sum(int(session.capture_channels[bit].samples[index]) << bit for bit in range(4)))
            for index in range(len(session.capture_channels[0].samples))]


def run(driver, session) -> object:
    done = threading.Event()
    results = []
    assert driver.start_capture(session, lambda args: (results.append(args), done.set())).name == "NONE"
    assert done.wait(20)
    return results[0]


def test_the_simulator_samples_on_the_clock_with_times():
    instrument, session = state_capture(40)
    result = run(instrument.capture.driver, session)
    assert result.success
    values = counter(session)
    assert all((later - earlier) % 16 == 1 for earlier, later in zip(values, values[1:]))  # one count per edge
    assert session.state_times[:3] == pytest.approx([0.0, 1.0, 2.0])  # µs, the clock is 1 MHz
    assert session.is_state_capture


def test_states_to_timing_keeps_each_state_until_the_next():
    session = CaptureSession(frequency=1, pre_trigger_samples=0, post_trigger_samples=3)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.array([0, 1, 0], np.uint8))]
    session.state_times = np.array([0.0, 2.0, 10.0])  # µs
    timing = states_to_timing(session, rate=1e6)
    assert timing.frequency == 1_000_000
    assert list(timing.capture_channels[0].samples) == [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0]


def test_files_keep_the_state_times(tmp_path):
    instrument, session = state_capture(20)
    run(instrument.capture.driver, session)
    capture_io.save_capture(str(tmp_path / "s.lac"), session)
    loaded = capture_io.load_capture(str(tmp_path / "s.lac")).session
    assert loaded.state_times == pytest.approx(session.state_times)
    capture_io.export_csv(str(tmp_path / "s.csv"), session, include_time=True)
    second = (tmp_path / "s.csv").read_text().splitlines()[2]
    assert second.startswith("1e-06,")  # the CSV time is the real time of the state


def test_ticks_count_states():
    ticks = time_axis.state_ticks(0, 1000, 960)
    labels = [tick.label for tick in ticks if tick.major]
    assert labels[:3] == ["#0", "#100", "#200"]


def test_the_analyzer_shows_states_per_state_and_on_real_time(make_dataview):
    instrument, session = state_capture(64)
    run(instrument.capture.driver, session)
    window = make_dataview()
    window.load_session(session)
    QApplication.processEvents()
    assert window.state_time_button.isVisible() or not window.state_time_button.isHidden()

    window.model.set_cursor("A", 2)
    window.model.set_cursor("B", 12)
    text = window.measure_panel.cursor_values["A"].text()
    assert text.startswith("state #2") and "2 µs" in text  # both: the state and its time
    assert window.measure_panel.cursor_values["delta"].text() == "10 µs"

    window.state_time_button.setChecked(True)
    shown = window.model.session
    assert shown is not session and shown.state_times is None and shown.frequency == 10_000_000
    assert window.model.sample_count == pytest.approx(630, abs=2)
    window.state_time_button.setChecked(False)
    assert window.model.session is session


def test_live_state_and_a_software_trigger_on_states(make_dataview):
    instrument, session = state_capture(30, stream=True)
    session.software_trigger = True
    session.trigger_type = TriggerType.EDGE
    session.trigger_channel = 3
    session.pre_trigger_samples = 4
    session.post_trigger_samples = 12
    window = make_dataview()
    window.use_instrument(instrument)
    window._begin_capture(session)
    deadline = time.monotonic() + 20
    while window.driver.is_capturing and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    for _ in range(20):
        QApplication.processEvents()
    shown = window.model.session
    pre = shown.pre_trigger_samples
    assert 1 <= pre <= 4 and shown.sample_count() == pre + 12  # fewer before it if it came early
    values = counter(shown)
    assert values[pre] == 8 and values[pre - 1] == 7  # bit 3 rose: the trigger
    assert shown.state_times is not None and shown.state_times[0] == 0
