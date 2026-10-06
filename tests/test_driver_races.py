"""Drivers when two things happen at once: a query while a capture starts, a capture started
right after one was stopped, a failing board in a set. (Against fake transports and devices –
not checked on hardware.)"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from openscilab import api
from openscilab.driver.base import ACQUISITION_STREAM, CaptureCompletedArgs, CaptureError
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.pico import multi
from openscilab.driver.simulated import open_simulated
from openscilab.driver.software_trigger import SoftwareTriggerDriver

from test_driver import driver, make_session  # noqa: F401 - fixture
from test_dslogic_driver import FakeDSLogic, info, open_driver, session
from test_multi import FakeDevice, NEW_FIRMWARE
from test_software_trigger import FakeStreamDriver


def wait_for(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# ------------------------------------------------------------------------ Pico
@pytest.mark.parametrize("query", ["capabilities", "device_details"])
def test_a_query_does_not_write_into_a_capture_that_just_started(driver, query):  # noqa: F811
    transport = driver.test_transport
    driver._capabilities = frozenset({"DEVICE_INFO"}) if query == "device_details" else None
    driver._details = None
    holding, release, answers = threading.Event(), threading.Event(), []

    def hold() -> None:
        with driver._lock:  # as start_capture does while it talks to the board
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    asker = threading.Thread(target=lambda: answers.append(getattr(driver, query)()))
    asker.start()
    time.sleep(0.1)  # the query waits for the lock
    written = len(transport.written)
    driver._capturing = True  # the capture started meanwhile
    release.set()
    asker.join(5)
    holder.join(5)
    assert answers in ([frozenset()], [{}])
    assert len(transport.written) == written  # nothing was sent into the capture
    driver._capturing = False


def test_a_stopped_buffer_capture_has_no_reader_left(driver):  # noqa: F811
    transport = driver.test_transport
    transport.queue_response("CAPTURE_STARTED")
    transport.queue_response("CAPTURE_DATA")
    results = []
    driver.start_capture(make_session(channels=1, pre=2, post=6), results.append)
    thread = driver._capture_thread
    assert driver.stop_capture()
    assert not thread.is_alive()  # it ended with the port it read from: none left for the next capture
    assert results == [] and transport.reopened == 1


# ----------------------------------------------------------------------- multi
def test_the_bootloader_command_reaches_every_board(monkeypatch):
    asked = []

    class Board(FakeDevice):
        def enter_bootloader(self):
            asked.append(self.name)
            return self.name != "board1"  # the first one does not answer

    devices = iter(Board(f"board{index + 1}", NEW_FIRMWARE, []) for index in range(3))
    monkeypatch.setattr(multi, "PicoDriver", lambda _connection: next(devices))
    driver = multi.MultiAnalyzerDriver(["/dev/0", "/dev/1", "/dev/2"])  # noqa: F811
    assert driver.enter_bootloader() is False
    assert asked == ["board1", "board2", "board3"]


def test_the_handler_of_a_set_is_called_without_its_lock(monkeypatch):
    started = []
    devices = iter(FakeDevice(f"board{index + 1}", NEW_FIRMWARE, started) for index in range(2))
    monkeypatch.setattr(multi, "PicoDriver", lambda _connection: next(devices))
    driver = multi.MultiAnalyzerDriver(["/dev/0", "/dev/1"])  # noqa: F811
    capture = CaptureSession(frequency=1_000_000, pre_trigger_samples=10, post_trigger_samples=90)
    capture.capture_channels = [AnalyzerChannel(channel_number=number) for number in (0, 24)]
    locked = []

    def completed(args: CaptureCompletedArgs) -> None:
        # another thread (a board reporting, the window stopping) can take the lock meanwhile
        other = threading.Thread(target=lambda: locked.append(driver._lock.acquire(timeout=1) and
                                                              (driver._lock.release() or True)))
        other.start()
        other.join(3)

    assert driver.start_capture(capture, completed) == CaptureError.NONE
    for index, (_name, device_session) in enumerate(started):
        for channel in device_session.capture_channels:
            channel.samples = np.zeros(100, np.uint8)
        driver._device_capture_completed(index, CaptureCompletedArgs(True, device_session))
    driver.wait_for_events()  # (the handlers run in the driver's notifier thread)
    assert locked == [True] and not driver.is_capturing

    # a board that fails: the handler hears it once, also without the lock
    locked.clear()
    assert driver.start_capture(capture, completed) == CaptureError.NONE
    driver._device_capture_completed(0, CaptureCompletedArgs(False, started[-2][1], error="gone"))
    driver._device_capture_completed(1, CaptureCompletedArgs(False, started[-1][1], error="gone too"))
    driver.wait_for_events()
    assert locked == [True] and not driver.is_capturing


# -------------------------------------------------------------- software trigger
def test_a_second_capture_does_not_take_the_watch_of_a_running_one():
    data = {0: np.zeros(50_000, np.uint8)}  # no edge: the first capture waits for its trigger
    inner = FakeStreamDriver(data, continuous=True)
    wrapper = SoftwareTriggerDriver(inner)
    first = CaptureSession(frequency=1_000_000, pre_trigger_samples=10, post_trigger_samples=100,
                           trigger_type=TriggerType.EDGE, trigger_channel=0, software_trigger=True,
                           acquisition_mode=ACQUISITION_STREAM)
    first.capture_channels = [AnalyzerChannel(channel_number=0)]
    results = []
    assert wrapper.start_capture(first, results.append) == CaptureError.NONE
    run = wrapper._run
    assert run is not None and wait_for(lambda: inner.is_capturing)
    second = CaptureSession(frequency=1_000_000, post_trigger_samples=100)
    second.capture_channels = [AnalyzerChannel(channel_number=0)]
    assert wrapper.start_capture(second, results.append) == CaptureError.BUSY
    assert wrapper._run is run  # still watched: it ends when its trigger comes or it is stopped
    wrapper.stop_capture()
    assert wait_for(lambda: not inner.is_capturing)


# --------------------------------------------------------------------- DSLogic
def test_a_stopped_dslogic_capture_leaves_the_next_one_alone():
    device = FakeDSLogic(info())
    driver = open_driver(device)  # noqa: F811
    results = []
    assert driver.start_capture(session([0]), results.append) is CaptureError.NONE
    old_thread, old_flag = driver._capture_thread, driver._abort
    assert driver.stop_capture()
    assert driver.start_capture(session([0]), results.append) is CaptureError.NONE
    assert driver._abort is not old_flag and old_flag.is_set() and not driver._abort.is_set()
    old_thread.join(5)
    # the reader of the stopped capture ended without marking the device idle or stopping it
    assert driver.is_capturing and results == []
    driver.stop_capture()
    driver.dispose()


# ------------------------------------------------------------------- simulator
def test_stopping_a_capture_does_not_interrupt_the_transfer_before_it():
    instrument = open_simulated("dho924s", fast=False)
    driver = instrument.capture.driver  # noqa: F811
    inner = getattr(driver, "inner", driver)
    capture = CaptureSession(frequency=100_000_000, pre_trigger_samples=0, post_trigger_samples=2_000_000,
                             trigger_type=TriggerType.IMMEDIATE)
    capture.capture_channels = [AnalyzerChannel(channel_number=0)]
    capture.analog_channels = [AnalogChannel(channel_number=0)]
    done = threading.Event()
    inner.fast = True
    assert driver.start_capture(capture, lambda args: done.set()) == CaptureError.NONE
    assert done.wait(10)
    transfer = inner._transfer_abort
    inner._abort.set()  # what stopping any capture does
    assert not transfer.is_set()
    assert wait_for(lambda: capture.progressive.complete, 30)
    assert not capture.progressive.interrupted
    inner.stop_transfer()
    assert inner._transfer_abort.is_set()


def test_a_script_gets_all_samples_of_a_progressive_capture():
    device = api.instrument("sim:dho924s") if hasattr(api, "instrument") else None
    scope = api.Device(device.capture.driver) if device is not None else None
    if scope is None:
        pytest.skip("no way to wrap a simulator as an API device")
    capture = CaptureSession(frequency=100_000_000, pre_trigger_samples=0, post_trigger_samples=3_000_000,
                             trigger_type=TriggerType.IMMEDIATE)
    capture.capture_channels = [AnalyzerChannel(channel_number=0)]
    capture.analog_channels = [AnalogChannel(channel_number=0)]
    result = scope.run(capture, timeout=30)
    assert result.session.progressive is None or result.session.progressive.complete
    assert result.session.analog_channels[0].raw.any()  # the samples, not the zeros they were before


# --------------------------------------------------------------------- settings
def test_settings_are_cloned_without_touching_the_channels():
    samples = np.ones(1000, np.uint8)
    capture = CaptureSession(frequency=1000, post_trigger_samples=1000)
    capture.capture_channels = [AnalyzerChannel(channel_number=0, samples=samples)]
    seen = []

    class Watched(AnalyzerChannel):
        def __setattr__(self, name, value):
            if name == "samples" and "samples" in self.__dict__:
                seen.append(value)
            super().__setattr__(name, value)

    capture.capture_channels.append(Watched(channel_number=1, samples=samples))
    clone = capture.clone_settings()
    assert [channel.samples for channel in clone.capture_channels] == [None, None]
    assert seen == []  # never set to None for a moment (another thread may be drawing it)
    assert capture.capture_channels[1].samples is samples


# ----------------------------------------------------------------- the window
def test_ports_are_read_in_a_thread(shell, monkeypatch):
    from openscilab.ui.shell import main_window

    readers = []
    monkeypatch.setattr(main_window, "serial_ports",
                        lambda: (readers.append(threading.current_thread()), frozenset({"/dev/x"}))[1])
    seen = []
    monkeypatch.setattr(shell, "watch_ports", lambda ports=None: seen.append(ports))
    shell._ports_read.disconnect()
    shell._ports_read.connect(shell.watch_ports)
    shell.poll_ports()
    from PySide6.QtWidgets import QApplication

    assert wait_for(lambda: (QApplication.processEvents(), bool(seen))[1])
    assert readers and readers[0] is not threading.current_thread()
    assert seen == [frozenset({"/dev/x"})]
