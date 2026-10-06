"""Devices stay usable: failures end a capture, signals survive outputs, nothing hangs or leaks."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from openscilab.driver.base import CaptureError
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated, scenarios
from openscilab.driver.simulated.circuit import Circuit, Counter, OutputSource
from openscilab.driver.simulated.device import _Growing


def session_of(channels, rate=1_000_000, pre=0, post=2000, **options) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=pre, post_trigger_samples=post, **options)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    return session


def run_capture(driver, session, timeout=10.0):
    done = threading.Event()
    results = []
    error = driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert error == CaptureError.NONE, error
    assert done.wait(timeout), "the capture never ended"
    return results[0]


# ------------------------------------------------------------------ simulator
def test_a_failing_source_ends_the_capture_and_the_device_stays_usable():
    instrument = open_simulated("free", fast=True)
    driver = instrument.simulated_driver

    class Broken(Counter):
        def digital(self, start, rate, count):
            raise RuntimeError("broken source")

    driver.circuit.drive("D0", Broken())
    result = run_capture(driver, session_of([0], trigger_type=TriggerType.IMMEDIATE))
    assert not result.success and "broken source" in result.error
    assert not driver.is_capturing
    driver.circuit.drive("D0", Counter(frequency=1e6))
    assert run_capture(driver, session_of([0], trigger_type=TriggerType.IMMEDIATE)).success


def test_wiring_loops_are_refused():
    circuit = Circuit()
    with pytest.raises(ValueError, match="loop"):
        circuit.apply_wiring([{"from": "D0", "to": "D1"}, {"from": "D1", "to": "D0"}])


def test_a_pin_shows_its_signal_again_after_being_an_output():
    instrument = open_simulated("pico", fast=True, clock=lambda: 1.0)
    driver = instrument.simulated_driver
    before = driver.circuit.digital("GP2", 2.0, 1e6, 64)
    assert before.any() and not before.all()  # the counter of the profile
    instrument.gpio.set_mode("GP2", "output")
    instrument.gpio.write("GP2", 1)
    assert driver.circuit.digital("GP2", 2.0, 1e6, 64).all()
    instrument.gpio.safe_all()
    assert np.array_equal(driver.circuit.digital("GP2", 2.0, 1e6, 64), before)
    assert "counter" in driver.circuit.describe()["GP2"]
    # a new scenario shows under an output as well
    instrument.gpio.write("GP3", 1)
    scenarios.apply(driver, {"scenario": "uart", "channel": 2})  # channel 2 is GP3
    assert driver.circuit.digital("GP3", 2.0, 1e6, 16).all()  # still driven
    instrument.gpio.safe_all()
    assert "UART" in driver.circuit.describe()["GP3"]


def test_an_output_is_read_consistently_while_it_changes():
    output = OutputSource()
    failures = []
    stop = threading.Event()

    def read():
        while not stop.is_set():
            try:
                output.digital(0.0, 1e6, 256)
            except Exception as error:  # noqa: BLE001
                failures.append(error)
                return

    reader = threading.Thread(target=read)
    reader.start()
    for step in range(400):
        output.set(step * 1e-6, step % 2)
        output.add(step * 1e-6 + 5e-7, 1 - step % 2)
        output.set_pwm(step * 1e-6 + 6e-7, 1000.0, 0.5)
    stop.set()
    reader.join(5)
    assert failures == []


def test_growing_keeps_views_valid_and_the_newest_samples():
    growing = _Growing(np.int16, keep=1000)
    first = None
    for block in range(50):
        growing.append(np.full(100, block, dtype=np.int16))
        if block == 4:
            first = growing.view()
            snapshot = first.copy()
    assert np.array_equal(first, snapshot)  # handed out earlier, never overwritten
    view = growing.view()
    assert len(view) == 1000 and view[0] == 40 and view[-1] == 49


def test_an_endless_analog_stream_does_not_grow_without_end():
    instrument = open_simulated("pico", fast=True)
    driver = instrument.simulated_driver
    session = CaptureSession(frequency=100_000, pre_trigger_samples=0, post_trigger_samples=2000,
                             trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream", continuous=True)
    session.capture_channels = [AnalyzerChannel(channel_number=0)]
    session.analog_channels = [AnalogChannel(channel_number=0)]
    sizes = []
    driver.add_capture_progress_handler(lambda args: sizes.append(len(args.analog[0])))
    result = run_capture(driver, session)
    assert result.success and max(sizes) <= 2000  # the newest samples only
    assert len(result.session.analog_channels[0].raw) <= 2000


def test_many_states_are_sampled_quickly():
    instrument = open_simulated("free", fast=True, clock=lambda: 0.0)
    driver = instrument.simulated_driver
    session = session_of(range(8), rate=4_000_000, post=20_000, trigger_type=TriggerType.IMMEDIATE)
    session.clock_channel = 8  # the 1 MHz clock of the profile
    started = time.monotonic()
    result = run_capture(driver, session, timeout=20)
    elapsed = time.monotonic() - started
    assert result.success and len(result.session.capture_channels[0].samples) == 20_000
    assert elapsed < 3.0  # one call per state and channel took far longer
    # the states are the counter: bit 0 toggles from state to state
    bit0 = result.session.capture_channels[0].samples
    assert np.all(bit0[1:] != bit0[:-1])


# ------------------------------------------------------------------------ hub
def test_a_failing_listener_does_not_break_the_hub():
    from openscilab.core.hub import Hub

    hub = Hub()
    seen = []
    hub.subscribe(lambda event: 1 / 0)
    hub.subscribe(lambda event: seen.append((event.kind, event.instrument is not None)))
    instrument = open_simulated("free")
    name = hub.add(instrument)
    hub.remove(name)
    assert seen == [("added", True), ("removed", True)] and len(hub) == 0


# ----------------------------------------------------------------- background
def wait_for(condition, timeout: float = 10.0) -> bool:
    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_slow_device_calls_do_not_freeze_the_window(shell):
    from PySide6.QtCore import QEvent, QTimer
    from PySide6.QtWidgets import QApplication

    from openscilab.ui import background

    handled = []

    def slow():
        time.sleep(0.5)
        return "opened"

    # what earlier tests left to be deleted is deleted by the first real event loop: not measured
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    started = time.monotonic()
    QTimer.singleShot(50, lambda: handled.append(time.monotonic()))
    assert background.run(shell, "Connecting...", slow) == "opened"
    # the window handled its events while the call was running (it was not frozen)
    assert handled and handled[0] - started < 0.45

    def failing():
        time.sleep(0.1)
        raise OSError("port busy")

    with pytest.raises(OSError, match="port busy"):
        background.run(shell, "Connecting...", failing)
    assert background.run(shell, "Connecting...", lambda: 5) == 5  # at once: no waiting at all


def test_cancelled_waiting_releases_what_comes_later(shell, monkeypatch):
    from openscilab.ui import background

    released = []

    class Driver:
        def dispose(self):
            released.append(True)

    def slow():
        time.sleep(0.3)
        return Driver()

    monkeypatch.setattr(background, "_wait", lambda *args: None)  # the user pressed Cancel at once
    with pytest.raises(background.Cancelled):
        background.run(shell, "Connecting...", slow)
    assert wait_for(lambda: released == [True])


def test_a_port_that_answers_slowly_is_opened_in_the_background(shell, monkeypatch):
    from openscilab.ui.devices import pico as pico_devices

    threads = []

    class SlowDriver:
        def __init__(self, connection):
            threads.append(threading.current_thread())
            time.sleep(0.2)
            raise OSError("no answer")

    from openscilab.driver.pico import analyzer

    monkeypatch.setattr(analyzer, "PicoDriver", SlowDriver)
    assert shell.connect_entry(pico_devices.serial_entry("/dev/cu.slow")) is None
    assert threads and threads[0] is not threading.main_thread()
    assert "no answer" in shell.devices_section.error_banner.label.text()


# -------------------------------------------------------------- stop and loss
def armed_card(shell):
    """A simulator waiting for an edge that never comes."""
    instrument = open_simulated("free")
    scenarios.apply(instrument.simulated_driver, {"scenario": "idle"})
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    session = session_of([0, 1], rate=1_000_000, pre=10, post=100, trigger_type=TriggerType.EDGE, trigger_channel=0)
    assert card.controller.capture(session)
    return instrument, card


def test_stopping_a_capture_that_waits_for_its_trigger_ends_it_everywhere(shell):
    instrument, card = armed_card(shell)
    view = card.controller.view
    state = view.capture_controls.state_label
    assert view.is_capturing and "Armed" in state.text()
    view.stop_run()
    assert wait_for(lambda: not view.is_capturing and not instrument.capture.driver.is_capturing)
    assert "Armed" not in state.text()
    assert view.capture_controls.capture_button.isEnabled()
    # and the next capture works
    assert card.controller.capture(session_of([0], post=100, trigger_type=TriggerType.IMMEDIATE))
    assert wait_for(lambda: view.model.session is not None and not card.controller.is_capturing)


def test_a_device_that_stops_answering_is_marked_and_can_be_reconnected(shell):
    from openscilab.core.instrument import InstrumentStatus
    from openscilab.driver.base import CaptureCompletedArgs
    from openscilab.ui.devices.capture import connection_error

    assert connection_error("Connection closed by the device")
    assert connection_error("[Errno 6] Device not configured")
    assert not connection_error("No trigger within 10 s of signal.")
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    card.controller._completed(CaptureCompletedArgs(success=False, session=None, error="Connection closed by the device"))
    assert instrument.status == InstrumentStatus.ERROR
    assert not card.reconnect_button.isHidden() and "does not answer" in card.banner.label.text()
    card.controller._completed(CaptureCompletedArgs(success=False, session=None, error="No trigger within 10 s"))
    assert instrument.status == InstrumentStatus.ERROR  # an ordinary failure changes nothing


def test_a_removed_device_takes_its_controller_along(shell):
    instrument = open_simulated("free")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    controller = card.controller
    shell.show_data(instrument)  # the data view captures: its quick settings are the controller's
    driver = instrument.simulated_driver
    assert controller.quick_settings is not None
    shell.hub.remove(instrument.name)
    assert wait_for(lambda: not hasattr(instrument, "capture_controller"))
    assert controller.quick_settings is None and not controller.power_timer.isActive()
    assert driver._capture_completed_handlers == [] and driver._event_listeners == []


def test_closing_the_card_leaves_a_recording_its_monitor(shell):
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    view = shell.record_monitor(instrument)
    assert instrument.monitor.running and card.monitor_box.isChecked()
    shell.area.close_document(card, force=True)
    assert instrument.monitor.running  # the recording started it, not the card
    view.stop_recording()


# --------------------------------------------------------------------- engine
def test_a_stopped_flow_leaves_the_devices_of_the_hub_idle(tmp_path):
    from openscilab.core.hub import Hub
    from openscilab.lab import Flow
    from openscilab.lab.engine import Engine

    hub = Hub()
    instrument = open_simulated("free", clock=hub.now)
    scenarios.apply(instrument.simulated_driver, {"scenario": "idle"})
    hub.add(instrument)
    flow = Flow("armed")
    flow.add_node("device.instrument", "sim", address="sim:free")
    flow.add_node("device.capture", "cap", channels=["D0"], rate="1 MHz", samples=1000,
                  trigger={"edge": "rising", "source": "D0"})  # an edge that never comes
    flow.connect("sim.device", "cap.device")
    engine = Engine(flow, mode="real", hub=hub, data_dir=str(tmp_path))
    thread = threading.Thread(target=engine.run)
    thread.start()
    driver = instrument.simulated_driver
    deadline = time.monotonic() + 5
    while not driver.is_capturing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert driver.is_capturing and engine.devices["sim"] is instrument
    engine.stop()
    thread.join(10)
    deadline = time.monotonic() + 5
    while driver.is_capturing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not driver.is_capturing  # the flow let go of the device
    assert instrument in hub  # and it stays open


def test_a_wait_that_cannot_be_cancelled_cannot_be_closed(qtbot):
    """Saving shows a dialog without *Cancel*; closing it by the title bar must not end the wait
    (the file is written all the same) or raise in the slot that saves."""
    import threading
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

    from openscilab.ui import background

    parent = QWidget()
    qtbot.addWidget(parent)
    release = threading.Event()
    closed = []

    def close_it() -> None:
        for dialog in QApplication.topLevelWidgets():
            if isinstance(dialog, QProgressDialog):
                closed.append(dialog.close())  # the title bar button
                dialog.reject()  # Escape
        release.set()

    QTimer.singleShot(150, close_it)
    started = time.monotonic()
    assert background.run(parent, "Saving...", lambda: release.wait(5) and "written", cancellable=False) == "written"
    assert closed == [False] and time.monotonic() - started < 3
