"""The software trigger (driver.software_trigger) on a fake streaming driver."""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np
import pytest

from pipilogicanalyzer.core.sample_store import RingStore, SampleStore
from pipilogicanalyzer.driver.base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_CONTINUOUS_STREAM,
    AnalyzerDriverBase,
    CaptureCompletedArgs,
    CaptureError,
    CaptureLimits,
    CaptureProgressArgs,
)
from pipilogicanalyzer.driver.models import (
    AnalyzerChannel,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)
from pipilogicanalyzer.driver.software_trigger import (
    NO_TRIGGER_ERROR,
    SoftwareTriggerDriver,
    supports_software_trigger,
)

from test_pico_stream import pico  # noqa: F401,F811 - fixture


class FakeStreamDriver(AnalyzerDriverBase):
    """Streams ``data`` in chunks from a thread, like the DSLogic and Pico stream drivers:
    progress with views of a (ring) store, stopped by ``stop_capture``, completion with the result."""

    def __init__(self, data: dict[int, np.ndarray], chunk: int = 997, continuous: bool = True,
                 stream: bool = True, budget: int = 10**9) -> None:
        super().__init__()
        self.data = data
        self.chunk = chunk
        self.continuous = continuous
        self.stream = stream
        self.budget = budget
        self.sessions: list[CaptureSession] = []
        self.stops = 0
        self.sent = 0
        self._capturing = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.custom_attribute = "inner"

    @property
    def device_version(self) -> Optional[str]:
        return "FAKE 1.0"

    @property
    def max_frequency(self) -> int:
        return 123_000_000

    @property
    def channel_count(self) -> int:
        return 16

    @property
    def buffer_size(self) -> int:
        return 1 << 20

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAPABILITY_CONTINUOUS_STREAM}) if self.continuous else frozenset()

    def acquisition_modes(self) -> tuple[str, ...]:
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM) if self.stream else ()

    def get_limits(self, channels, acquisition_mode=None, *, to_disk=False, continuous=False) -> CaptureLimits:
        return CaptureLimits(0, 0, 1, self.budget)

    def start_capture(self, session, completed_handler=None) -> CaptureError:
        self.sessions.append(session)
        if session.acquisition_mode != ACQUISITION_STREAM:
            # a buffer capture completes at once with the data
            for channel in session.capture_channels:
                channel.samples = self.data[channel.channel_number].copy()
            self._raise_capture_completed(CaptureCompletedArgs(True, session), completed_handler)
            return CaptureError.NONE
        assert session.trigger_type == TriggerType.IMMEDIATE
        assert session.continuous == self.continuous
        self._capturing = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(session, completed_handler), daemon=True)
        self._thread.start()
        return CaptureError.NONE

    def _run(self, session, handler) -> None:
        numbers = session.channel_numbers
        wanted = session.post_trigger_samples
        store = RingStore(numbers, wanted) if session.continuous else SampleStore(numbers, wanted)
        total = len(next(iter(self.data.values())))
        position = 0
        while position < total and not self._stop.is_set():
            if not session.continuous and store.total >= wanted:
                break
            end = min(position + self.chunk, total)
            store.append({n: self.data[n][position:end] for n in numbers})
            self.sent = end
            position = end
            views, first = store.window()
            self._raise_capture_progress(CaptureProgressArgs(session, views, first))
            self._stop.wait(0.0005)
        samples, first = store.result()
        for channel in session.capture_channels:
            channel.samples = samples[channel.channel_number]
        self._capturing = False
        self._raise_capture_completed(CaptureCompletedArgs(True, session, first_sample=first), handler)

    def stop_capture(self) -> bool:
        self.stops += 1
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2)
        return True


def make_session(trigger=TriggerType.EDGE, pre=100, post=200, channels=(0, 1), rate=1_000_000) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=pre, post_trigger_samples=post)
    session.capture_channels = [AnalyzerChannel(channel_number=n) for n in channels]
    session.trigger_type = trigger
    session.software_trigger = True
    return session


def capture(driver: SoftwareTriggerDriver, session: CaptureSession, expect=CaptureError.NONE):
    done = threading.Event()
    results: list[CaptureCompletedArgs] = []
    progress: list[CaptureProgressArgs] = []
    driver.add_capture_progress_handler(progress.append)
    error = driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert error is expect, driver.last_error
    if expect is CaptureError.NONE:
        assert done.wait(10)
    return results[0] if results else None, progress


def data_with_rise(count: int, position: int) -> dict[int, np.ndarray]:
    rng = np.random.default_rng(5)
    noise = rng.integers(0, 2, count).astype(np.uint8)
    trigger = np.zeros(count, dtype=np.uint8)
    trigger[position:] = 1
    return {0: trigger, 1: noise}


@pytest.mark.parametrize("continuous", [True, False])
@pytest.mark.parametrize("chunk", [13, 997, 100_000])
def test_edge_trigger_trims_around_the_trigger(continuous, chunk):
    data = data_with_rise(200_000, 50_000)
    inner = FakeStreamDriver(data, chunk=chunk, continuous=continuous)
    if chunk < 100:
        inner.data = data = {n: values[40_000:60_000] for n, values in data.items()}
    driver = SoftwareTriggerDriver(inner)
    session = make_session(pre=1000, post=2000)
    result, progress = capture(driver, session)
    assert result.success, result.error
    assert result.session is session
    assert session.trigger_type == TriggerType.EDGE
    assert session.pre_trigger_samples == 1000 and session.post_trigger_samples == 2000
    trigger = int(np.argmax(data[0]))
    assert result.first_sample == trigger - 1000
    for channel in session.capture_channels:
        np.testing.assert_array_equal(channel.samples, data[channel.channel_number][trigger - 1000:trigger + 2000])
    stream = inner.sessions[0]
    assert stream is not session and stream.acquisition_mode == ACQUISITION_STREAM
    assert stream.trigger_type == TriggerType.IMMEDIATE and not stream.software_trigger
    if chunk < 100_000:
        assert inner.stops == 1 and inner.sent < len(data[0])  # stopped once the samples were there
    assert progress and all(args.session is session for args in progress)


def test_fewer_pre_trigger_samples_when_the_trigger_comes_early():
    data = data_with_rise(10_000, 30)
    driver = SoftwareTriggerDriver(FakeStreamDriver(data, chunk=64))
    session = make_session(pre=100, post=50)
    result, _ = capture(driver, session)
    assert result.success
    assert (session.pre_trigger_samples, session.post_trigger_samples) == (30, 50)
    np.testing.assert_array_equal(session.capture_channels[1].samples, data[1][:80])


def test_ring_buffer_keeps_the_samples_around_the_trigger():
    # a small margin and ring: the stream drops old samples long before the trigger
    data = data_with_rise(300_000, 250_000)
    inner = FakeStreamDriver(data, chunk=500)
    driver = SoftwareTriggerDriver(inner)
    session = make_session(pre=300, post=300, rate=2000)  # margin of 1000 samples
    result, progress = capture(driver, session)
    assert result.success
    assert inner.sessions[0].post_trigger_samples == 1600
    assert max(args.first_sample for args in progress) > 0
    assert result.first_sample == 249_700
    np.testing.assert_array_equal(session.capture_channels[1].samples, data[1][249_700:250_300])


def test_sequence_with_pulse_and_pattern():
    count = 50_000
    a = np.zeros(count, dtype=np.uint8)
    b = np.zeros(count, dtype=np.uint8)
    a[1000:1003] = 1  # too short
    a[5000:5010] = 1  # 10 µs pulse ends at 5010
    b[3000:3100] = 1  # pattern before the pulse: ignored
    b[7000:8000] = 1  # pattern a=0, b=1 from 7000
    sequence = TriggerSequence([
        TriggerStage(TriggerCondition(kind=ConditionKind.PULSE, channel=0, edge=EdgeKind.RISING, min_ns=8000)),
        TriggerStage(TriggerCondition(kind=ConditionKind.PATTERN, mask=0b11, value=0b10), within_ns=5_000_000),
    ])
    driver = SoftwareTriggerDriver(FakeStreamDriver({0: a, 1: b}, chunk=333))
    session = make_session(TriggerType.SEQUENCE, pre=10, post=10)
    session.trigger_sequence = sequence
    result, _ = capture(driver, session)
    assert result.success
    assert result.first_sample == 6990
    assert session.capture_channels[1].samples.tolist() == [0] * 10 + [1] * 10


def test_no_trigger_reports_an_error():
    data = data_with_rise(5000, 5000)  # never rises
    driver = SoftwareTriggerDriver(FakeStreamDriver(data, chunk=100))
    result, _ = capture(driver, make_session())
    assert not result.success
    assert result.error.startswith(NO_TRIGGER_ERROR)


def test_user_stop_before_the_trigger():
    data = data_with_rise(10**7, 10**7)
    inner = FakeStreamDriver(data, chunk=1000)
    driver = SoftwareTriggerDriver(inner)
    done = threading.Event()
    results = []
    assert driver.start_capture(make_session(), lambda args: (results.append(args), done.set())) is CaptureError.NONE
    driver.stop_capture()
    assert done.wait(5)
    assert not results[0].success and "stopped" in results[0].error


def test_without_software_trigger_the_capture_passes_through():
    data = data_with_rise(1000, 10)
    inner = FakeStreamDriver(data)
    driver = SoftwareTriggerDriver(inner)
    session = make_session()
    session.software_trigger = False
    received = []
    driver.add_capture_completed_handler(received.append)
    assert driver.start_capture(session) is CaptureError.NONE
    assert inner.sessions == [session]
    assert received and received[0].session is session and len(session.capture_channels[0].samples) == 1000


def test_invalid_trigger_is_rejected():
    driver = SoftwareTriggerDriver(FakeStreamDriver(data_with_rise(100, 10)))
    session = make_session()
    session.trigger_channel = 5  # not captured
    capture(driver, session, CaptureError.BAD_PARAMS)
    assert "not captured" in driver.last_error
    too_many = make_session(pre=10**9, post=10**9)
    capture(driver, too_many, CaptureError.BAD_PARAMS)
    assert driver.inner.sessions == []


def test_device_without_stream():
    inner = FakeStreamDriver(data_with_rise(100, 10), stream=False)
    assert not supports_software_trigger(inner)
    assert supports_software_trigger(FakeStreamDriver(data_with_rise(100, 10)))
    capture(SoftwareTriggerDriver(inner), make_session(), CaptureError.UNSUPPORTED)


def test_delegation():
    inner = FakeStreamDriver(data_with_rise(100, 10))
    driver = SoftwareTriggerDriver(inner)
    assert driver.inner is inner
    assert driver.device_version == "FAKE 1.0"
    assert driver.max_frequency == 123_000_000
    assert driver.min_frequency == inner.min_frequency
    assert driver.channel_count == 16
    assert driver.capabilities() == inner.capabilities()
    assert driver.acquisition_modes() == inner.acquisition_modes()
    assert driver.get_limits([0, 1], ACQUISITION_STREAM).max_post_samples == 10**9
    assert driver.custom_attribute == "inner"
    assert not driver.is_capturing
    with pytest.raises(AttributeError):
        driver.missing_attribute
    driver.dispose()
    assert inner._capture_progress_handlers == []


def test_on_the_pico_stream_driver(pico):  # noqa: F811 - the imported fixture
    from test_pico_stream import chunks

    transport = pico.test_transport
    rng = np.random.default_rng(3)
    words = (rng.integers(0, 4, 5000) & 0b10).astype(np.uint8)  # bit 1: noise
    words[3000:] |= 1  # bit 0 rises at 3000
    transport.queue_response("STREAM_STARTED:0,1")
    transport.queue_data(chunks(words, size=128))
    driver = SoftwareTriggerDriver(pico)
    session = make_session(pre=100, post=200, rate=100_000)
    result, _ = capture(driver, session)
    assert result.success, result.error
    assert transport.stops == 1 and transport.reopened == 0
    assert (session.pre_trigger_samples, session.post_trigger_samples, result.first_sample) == (100, 200, 2900)
    np.testing.assert_array_equal(session.capture_channels[1].samples, (words[2900:3200] >> 1) & 1)
