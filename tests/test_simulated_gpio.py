"""The simulated GPIO: latency, exact pulses, PWM through an RC filter, the watchdog, reserved pins,
faults, the monitor and the GPIO flow nodes."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from openscilab.core.instrument import MODE_INPUT, MODE_OUTPUT, AnalogInFacet, InstrumentError
from openscilab.core.monitor_recording import MonitorRecording
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated


class Clock:
    def __init__(self) -> None:
        self.time = 0.0

    def __call__(self) -> float:
        return self.time


@pytest.fixture
def uno():
    clock = Clock()
    instrument = open_simulated("uno", clock=clock, fast=True)
    return instrument, clock


def test_the_uno_has_gpio_monitor_and_analog_facets(uno):
    instrument, _clock = uno
    assert instrument.gpio is not None and instrument.monitor is not None
    pins = {pin.name: pin for pin in instrument.pins()}
    assert pins["D0"].reserved == "USB serial (RX)" and not pins["D0"].usable
    assert "PWM" in pins["D9"].capabilities and "PWM" not in pins["D7"].capabilities
    assert "ADC" in pins["A0"].capabilities


def test_d7_is_wired_to_d3_with_the_latency(uno):
    instrument, clock = uno
    gpio = instrument.gpio
    gpio.set_mode("D7", MODE_OUTPUT)
    clock.time = 0.1
    gpio.write("D7", 1)  # takes effect 2 ms later (USB round trip)
    driver = instrument.simulated_driver
    names = driver.channel_names()
    levels = driver.circuit.digital("D3", 0.1, 10_000, 40)
    assert levels[0] == 0 and levels[-1] == 1
    assert int(np.argmax(levels)) == 20  # 2 ms at 10 kSa/s
    assert "D3" in names


def test_a_pulse_is_exact(uno):
    instrument, clock = uno
    clock.time = 0.5
    instrument.gpio.pulse("D7", 0.001)
    levels = instrument.simulated_driver.circuit.digital("D3", 0.5, 1_000_000, 5000)
    assert int(levels.sum()) == 1000  # exactly 1 ms, whatever the computer does meanwhile


def test_a_pulse_train_is_exact(uno):
    instrument, clock = uno
    clock.time = 0.5
    instrument.gpio.pulse("D7", 0.0002, count=3, period=0.001)
    levels = instrument.simulated_driver.circuit.digital("D3", 0.5, 1_000_000, 5000)
    assert int(levels.sum()) == 600
    rising = np.flatnonzero(np.diff(levels.astype(int)) == 1)
    assert np.diff(rising).tolist() == [1000, 1000]
    with pytest.raises(InstrumentError):
        instrument.gpio.pulse("D7", 0.001, count=2, period=0.0005)


def test_pwm_through_the_rc_filter_rises_at_a0(uno):
    instrument, clock = uno
    instrument.gpio.pwm("D9", 1000, 0.6)
    readings = []
    for at in (0.05, 0.1, 0.2, 0.6):
        clock.time = at
        instrument.gpio.heartbeat()
        readings.append(instrument.facet(AnalogInFacet).read(["A0"])["A0"])
    assert readings == sorted(readings)
    assert readings[0] < 1.6 and readings[-1] == pytest.approx(3.0, abs=0.1)  # 60 % of 5 V, τ = 0.1 s


def test_reserved_pins_and_missing_capabilities(uno):
    instrument, _clock = uno
    with pytest.raises(InstrumentError, match="reserved"):
        instrument.gpio.write("D0", 1)
    with pytest.raises(InstrumentError, match="D7 is no PWM output"):
        instrument.gpio.pwm("D7", 1000, 0.5)


def test_pins_of_a_running_capture_are_reserved(uno):
    instrument, clock = uno
    driver = instrument.capture.driver
    session = CaptureSession(frequency=10_000, pre_trigger_samples=0, post_trigger_samples=1000,
                             trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream")
    session.capture_channels = [AnalyzerChannel(channel_number=1)]  # D3
    model = instrument.simulated_driver
    model.fast = False
    done = threading.Event()
    clock.time = 0.0
    assert driver.start_capture(session, lambda args: done.set()).name == "NONE"
    try:
        pins = {pin.name: pin for pin in instrument.gpio.pins()}
        assert not pins["D3"].usable and "while capturing" in pins["D3"].reserved
        assert pins["D7"].usable
    finally:
        clock.time = 1.0
        driver.stop_capture()
        done.wait(5)


def test_the_watchdog_releases_the_outputs(uno):
    instrument, clock = uno
    gpio = instrument.gpio
    gpio.keepalive = False  # the host stopped sending its heartbeat (crashed, hangs)
    gpio.write("D7", 1)
    clock.time = 0.5
    gpio.heartbeat()
    clock.time = 1.2
    assert instrument.monitor.sample(["D3"]).digital["D3"] == 1  # still within a second of contact
    clock.time = 2.0
    state = instrument.monitor.sample(["D3"])
    assert state.digital["D3"] == 0 and gpio.mode("D7") == MODE_INPUT and gpio.watchdog_fired
    assert any("watchdog" in text for _stamp, text in instrument.simulated_driver.events)


def test_faults_disconnect_and_delay(uno):
    instrument, clock = uno
    model = instrument.simulated_driver
    model.inject("disconnect")
    with pytest.raises(InstrumentError, match="disconnected"):
        instrument.gpio.write("D7", 1)
    model.inject("reconnect")
    model.inject("delay", 0.05)
    clock.time = 1.0
    instrument.gpio.write("D7", 1)
    levels = model.circuit.digital("D3", 1.0, 1000, 60)
    assert int(np.argmax(levels)) == 52  # 2 ms latency + 50 ms delay


def test_the_button_bounces(uno):
    instrument, _clock = uno
    button = instrument.simulated_driver.circuit.sources["D2"]
    button.press(0.1, 0.05)
    levels = instrument.simulated_driver.circuit.digital("D2", 0.099, 1_000_000, 5000).astype(int)
    assert levels[0] == 1 and levels[-1] == 0  # active low
    assert np.count_nonzero(np.diff(levels)) > 2  # bounces, not one clean edge


def test_the_monitor_reports_and_records(uno):
    instrument, clock = uno
    recording = MonitorRecording(10, ["D3"], ("A0",))
    remove = instrument.monitor.on_state(recording.add)
    instrument.gpio.write("D7", 1)
    for step in range(5):
        clock.time = step * 0.1
        instrument.monitor._emit(instrument.monitor.sample(["D3"], ("A0",)))
    remove()
    session = recording.session()
    assert session.frequency == 10 and session.sample_count() == 5
    assert list(session.capture_channels[0].samples) == [1, 1, 1, 1, 1]
    assert session.analog_channels[0].volts()[0] == pytest.approx(0.0, abs=0.05)
    assert recording.sample_at(0.25) == 3


def test_a_running_monitor_thread(uno):
    instrument, _clock = uno
    states = []
    got = threading.Event()
    instrument.monitor.on_state(lambda state: (states.append(state), len(states) >= 3 and got.set()))
    instrument.monitor.start(200, ["D3"])
    try:
        assert got.wait(5)
    finally:
        instrument.monitor.stop()
    assert not instrument.monitor.running


def test_gpio_flow_nodes():
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    flow = yaml_io.loads("""
flow: Blink
nodes:
  uno: {type: device.instrument, address: "sim:uno"}
  out:   {type: gpio.write, pin: D7, level: 1}
  pwm:   {type: gpio.pwm, pin: D9, freq: 1 kHz, duty: 0.6}
  wait:  {type: control.timer, delay: 0.5 s, count: 1}
  d3:    {type: gpio.read, pin: D3}
  a0:    {type: gpio.read, pin: A0, analog: true}
  mon:   {type: device.monitor, pins: [D3], rate: 10 Hz, duration: 0.2 s}
  pulse: {type: gpio.pulse, pin: D8, width: 5 ms}
edges:
  - uno.device -> out.device
  - uno.device -> pwm.device
  - uno.device -> d3.device
  - uno.device -> a0.device
  - uno.device -> mon.device
  - uno.device -> pulse.device
  - wait.tick -> d3.trigger
  - wait.tick -> a0.trigger
""")
    seen = {}
    engine = Engine(flow, mode="virtual")
    engine.subscribe(lambda event: event.kind == "value" and seen.setdefault(f"{event.node}.{event.port}", []).append(
        event.value))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    assert seen["d3.value"][-1].value == 1.0
    assert seen["a0.value"][-1].value == pytest.approx(3.0 * (1 - np.exp(-5)), abs=0.1)
    assert [value.value for value in seen["mon.D3"]] == [1.0, 1.0]  # 0.2 s at 10 Hz: two reports
    assert seen["pulse.done"][0].data == [0.005]
