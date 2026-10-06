"""The simulator basics: profile ``free``, sources, buffer and stream, real time and fast."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from openscilab.core.instrument import InstrumentStatus
from openscilab.driver.base import CaptureError
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import available_profiles, load_profile, open_simulated
from openscilab.driver.simulated.circuit import Circuit, Counter, Square, Uart, make_source


def capture(driver, session, timeout=5.0):
    done = threading.Event()
    results = []
    error = driver.start_capture(session, lambda args: (results.append(args), done.set()))
    if error != CaptureError.NONE:
        return error
    assert done.wait(timeout)
    return results[0]


def make_session(channels, rate=1_000_000, pre=0, post=1000, **options) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=pre, post_trigger_samples=post, **options)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    return session


@pytest.fixture
def clock():
    class Clock:
        time = 0.0

        def __call__(self):
            return self.time

    return Clock()


def test_the_free_profile_is_built_in():
    assert "free" in available_profiles()
    profile = load_profile("free")
    assert len(profile["digital"]) == 16
    instrument = open_simulated("free")
    assert instrument.status == InstrumentStatus.SIMULATED and instrument.uri == "sim:free"
    assert instrument.kind == "Simulation: free"
    assert instrument.capture.channel_count == 16
    with pytest.raises(ValueError):
        load_profile("nothing")


def test_sources_are_functions_of_time():
    square = Square(frequency=1000, duty=0.25)
    assert list(square.digital(0.0, 4000, 8)) == [1, 0, 0, 0, 1, 0, 0, 0]
    counter = Counter(frequency=1000, bit=1)
    assert list(counter.digital(0.0, 1000, 8)) == [0, 0, 1, 1, 0, 0, 1, 1]
    # The same interval gives the same samples, wherever it is cut.
    whole = counter.digital(0.0, 1000, 8)
    assert np.array_equal(np.concatenate([counter.digital(0.0, 1000, 3), counter.digital(0.003, 1000, 5)]), whole)
    uart = Uart(text="A", baud=1000, gap=2)
    # idle, idle, start, 0x41 = 1000 0010 LSB first, stop
    assert list(uart.digital(0.0, 1000, 12)) == [1, 1, 0, 1, 0, 0, 0, 0, 0, 1, 0, 1]
    assert make_source({"type": "square", "frequency": "2 kHz"}).frequency == 2000
    with pytest.raises(ValueError):
        make_source({"type": "laser"})
    circuit = Circuit.from_description({"sources": {"X": {"type": "clock", "frequency": "1 kHz"}, "Y": 1}})
    assert list(circuit.digital("Y", 0, 1000, 2)) == [1, 1]
    assert list(circuit.digital("unconnected", 0, 1000, 2)) == [0, 0]


def test_buffer_capture_with_an_edge_trigger(clock):
    instrument = open_simulated("free", clock=clock, fast=True)
    driver = instrument.simulated_driver
    clock.time = 0.0123  # captures start at the device's time
    session = make_session([15], rate=100_000, pre=10, post=290, trigger_type=TriggerType.EDGE, trigger_channel=15,
                           trigger_inverted=True)
    result = capture(driver, session)
    assert result.success
    samples = session.capture_channels[0].samples
    assert samples[9] == 1 and samples[10] == 0  # the falling edge at the trigger position
    # from 12.3 ms + 10 pre-trigger samples on, the 1 kHz square falls at 12.5 ms
    assert driver.last_trigger_time == pytest.approx(0.0125, abs=1e-9)
    assert driver.last_capture_end == pytest.approx(0.0125 + 290 / 100_000, abs=1e-9)


def test_pattern_trigger_and_limits(clock):
    driver = open_simulated("free", clock=clock, fast=True).simulated_driver
    session = make_session(range(4), rate=4_000_000, pre=0, post=64, trigger_type=TriggerType.COMPLEX,
                           trigger_channel=0, trigger_bit_count=4, trigger_pattern=0b0101)
    assert capture(driver, session).success
    first = [int(session.capture_channels[bit].samples[0]) for bit in range(4)]
    assert first == [1, 0, 1, 0]
    too_long = make_session([0], post=driver.buffer_size + 1, trigger_type=TriggerType.IMMEDIATE)
    assert capture(driver, too_long) == CaptureError.BAD_PARAMS
    too_fast = make_session([0], rate=driver.max_frequency * 2, trigger_type=TriggerType.IMMEDIATE)
    assert capture(driver, too_fast) == CaptureError.BAD_PARAMS


def test_stream_in_blocks_fast_and_limited_by_the_bandwidth(clock):
    driver = open_simulated("free", clock=clock, fast=True).simulated_driver
    progress = []
    driver.add_capture_progress_handler(lambda args: progress.append(args.sample_count))
    session = make_session([0, 15], rate=100_000, post=3000, trigger_type=TriggerType.IMMEDIATE,
                           acquisition_mode="stream")
    result = capture(driver, session)
    assert result.success and len(session.capture_channels[1].samples) == 3000
    assert progress == [1000, 2000, 3000]
    limit = driver.stream_rate_limit([0])
    too_fast = make_session([0], rate=limit + 1, post=100, trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream")
    assert capture(driver, too_fast) == CaptureError.BAD_PARAMS


def test_real_time_takes_as_long_as_the_signal():
    driver = open_simulated("free").simulated_driver
    session = make_session([0], rate=10_000, post=1000, trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream")
    started = time.monotonic()
    assert capture(driver, session).success
    assert time.monotonic() - started >= 0.09


def test_an_endless_stream_stops_on_request():
    driver = open_simulated("free").simulated_driver
    session = make_session([15], rate=10_000, post=500, trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream",
                           continuous=True)
    done = threading.Event()
    results = []
    assert driver.start_capture(session, lambda args: (results.append(args), done.set())) == CaptureError.NONE
    time.sleep(0.15)
    assert driver.stop_capture()
    assert done.wait(5)
    assert results[0].success and len(session.capture_channels[0].samples) == 500  # the newest samples
    assert results[0].first_sample > 0


def test_the_lab_runs_without_qt():
    code = (
        "import sys\n"
        "from openscilab.lab import yaml_io\n"
        "from openscilab.lab.engine import Engine\n"
        "import tempfile\n"
        "flow = yaml_io.load('examples/flows/counter.flow.yaml')\n"
        "assert Engine(flow, mode='virtual', data_dir=tempfile.mkdtemp()).run().ok\n"
        "bad = [m for m in sys.modules if m.startswith('PySide6.QtWidgets') or m.startswith('openscilab.ui')]\n"
        "print(bad); sys.exit(1 if bad else 0)\n"
    )
    import os

    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
