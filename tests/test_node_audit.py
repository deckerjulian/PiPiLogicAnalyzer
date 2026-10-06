"""What the review of every node and simulator found: streams, units, time and the simulated devices."""

from __future__ import annotations

import numpy as np
import pytest
from test_lab_nodes import node_registry, run_node

from openscilab.core import units
from openscilab.core.signals import Analog, Digital, Scalar, TimeBase
from openscilab.driver.base import CaptureError
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated
from openscilab.driver.simulated.circuit import _low_pass
from openscilab.lab import yaml_io
from openscilab.lab.engine import Engine


@pytest.fixture
def registry():
    return node_registry()


def run(text: str, mode: str = "virtual"):
    """Runs a flow; returns the result and the values by ``node.port``."""
    engine = Engine(yaml_io.loads(text), mode=mode)
    seen: dict = {}
    engine.subscribe(lambda event: event.kind == "value" and seen.setdefault(f"{event.node}.{event.port}", []).append(
        event.value))
    return engine.run(timeout=30), seen


# --------------------------------------------------------------- units
def test_quantities_in_the_unit_of_a_signal():
    assert units.scale_of("mA") == (1e-3, "A") and units.scale_of("kHz") == (1e3, "Hz")
    assert units.scale_of("%") == (0.01, "%") and units.scale_of("mm") == (1.0, "mm")
    assert units.in_unit("20 mA", "mA") == 20 and units.in_unit("0.02 A", "mA") == 20
    assert units.in_unit(20, "mA") == 20  # a plain number is in the signal's unit
    with pytest.raises(units.UnitError):
        units.in_unit("3 V", "mA")


def test_compare_limit_and_threshold_take_prefixed_units(registry):
    current = Scalar(value=15.0, unit="mA")
    assert run_node(registry, "control.compare", {"a": [current]}, outputs=("result",), op="<",
                    value="20 mA")["result"][0].value
    assert run_node(registry, "control.compare", {"a": [Scalar(value=3.29, unit="V")]}, outputs=("result",),
                    op="within", value="3.3 V", tolerance="50 mV")["result"][0].value
    assert run_node(registry, "control.compare", {"a": ["10 ms"]}, outputs=("result",), op="startswith",
                    value="10")["result"][0].value  # (text stays text)
    limited = run_node(registry, "control.limit", {"in": [Scalar(value=25.0, unit="mA")]}, outputs=("ok", "out"),
                       high="0.02 A")
    assert not limited["ok"][0].value and limited["out"][0].value == 20.0 and limited["out"][0].unit == "mA"
    analog = Analog(values=[0.005, 0.015], unit="A", time=TimeBase.uniform(10))
    digital = run_node(registry, "dsp.threshold", {"in": [analog]}, threshold="10 mA")["out"][0]
    assert list(digital.values) == [0, 1]


# ------------------------------------------------------------- streams
def test_debounce_counts_a_change_across_two_blocks(registry):
    # a low pulse of 6 ms at 1 kHz split by the block boundary: longer than 5 ms, so it is kept
    first = Digital(values=[1] * 10 + [0] * 3, time=TimeBase.uniform(1000, 0.0))
    second = Digital(values=[0] * 3 + [1] * 10, time=TimeBase.uniform(1000, 0.013))
    out = run_node(registry, "dsp.debounce", {"in": [first, second]}, time="5 ms")["out"]
    assert np.concatenate([block.values for block in out]).min() == 0


def test_math_pairs_the_blocks_of_two_signals_by_time(registry):
    a = [Analog(values=np.ones(10) * index, time=TimeBase.uniform(1000, index * 0.01)) for index in range(3)]
    b = [Analog(values=np.ones(10), time=TimeBase.uniform(1000, index * 0.01)) for index in range(3)]
    out = run_node(registry, "dsp.math", {"a": a, "b": b}, expression="a + b")["out"]
    starts = [round(block.time.start, 6) for block in out]
    assert starts == sorted(set(starts)) and len(out) == 3  # each piece of time once
    assert [float(block.values[0]) for block in out] == [1.0, 2.0, 3.0]
    smallest = run_node(registry, "dsp.math", {"a": [Analog(values=[3.0, -2.0, 1.0], time=TimeBase.uniform(10))]},
                        expression="min(a)")["out"][0]
    assert smallest.value == -2.0 and smallest.unit == "V"


def test_resample_keeps_its_grid_across_blocks(registry):
    blocks = [Analog(values=np.arange(7, dtype=float) + 7 * index, time=TimeBase.uniform(1000, index * 0.007))
              for index in range(4)]
    out = run_node(registry, "dsp.resample", {"in": blocks}, rate="300 Hz")["out"]
    times = np.concatenate([block.times() for block in out])
    assert np.allclose(np.diff(times), 1 / 300)  # no gap or overlap where the blocks meet


def test_a_filter_is_placed_without_its_delay_and_skips_empty_blocks(registry):
    step = Analog(values=np.r_[np.zeros(1000), np.ones(1000)], time=TimeBase.uniform(10_000))
    empty = Analog(values=np.zeros(0), time=TimeBase.uniform(10_000, 0.2))
    out = run_node(registry, "dsp.filter", {"in": [step, empty]}, kind="lowpass", cutoff="500 Hz", taps=101)["out"]
    assert len(out) == 1  # (nothing for the empty block)
    crossing = out[0].times()[np.argmax(out[0].values >= 0.5)]
    assert crossing == pytest.approx(0.1, abs=2e-4)  # where the step is, not 5 ms later
    with pytest.raises(AssertionError, match="half the sample rate"):
        run_node(registry, "dsp.filter", {"in": [step]}, kind="lowpass", cutoff="6 kHz")


def test_measurements_keep_a_rate_that_is_no_whole_number(registry):
    signal = Digital(values=[0, 1] * 20, time=TimeBase.uniform(2.5))
    assert run_node(registry, "measure.frequency", {"in": [signal]})["out"][0].value == pytest.approx(1.25)


def test_setup_and_hold_go_by_time(registry):
    clock = Digital(values=([0] * 10 + [1] * 10) * 5, time=TimeBase.uniform(1e6))
    # the data changes 5 µs before every rising clock edge, sampled at 2 MHz
    data = Digital(values=([0] * 10 + [1] * 20 + [0] * 10) * 5, time=TimeBase.uniform(2e6))
    out = run_node(registry, "measure.setup_hold", {"data": [data], "clock": [clock]}, outputs=("setup", "hold"))
    assert out["setup"][-1].value == pytest.approx(5e-6, abs=1.1e-6)


# --------------------------------------------------------- state machine
def test_a_state_machine_ignores_what_came_before_a_state(registry):
    flow = yaml_io.loads("""
flow: t
nodes:
  button: {type: test.events, data: [1]}
  machine:
    type: control.state_machine
    initial: red
    states:
      red: {on: [{after: 20 ms, to: green}]}
      green: {on: [{input: press, to: yellow}, {after: 50 ms, to: done}]}
      yellow: {}
      done: {}
edges:
  - button.out -> machine.press
""")
    engine = Engine(flow, registry=registry, mode="virtual")
    states = []
    engine.subscribe(lambda event: event.kind == "value" and event.port == "state" and states.append(event.value))
    assert engine.run(timeout=20).ok
    assert states == ["red", "green", "done"]  # the press at 10 ms came while it was red


def test_a_transition_without_a_way_out_is_refused(registry):
    flow = yaml_io.loads("""
flow: t
nodes:
  machine: {type: control.state_machine, initial: a, states: {a: {on: [{to: b}]}, b: {}}}
""")
    result = Engine(flow, registry=registry, mode="virtual").run(timeout=20)
    assert result.state == "error" and "needs 'after'" in result.error


# ------------------------------------------------- devices in virtual time
def test_a_virtual_capture_sees_a_stimulus_after_a_wait():
    result, seen = run("""
flow: t
nodes:
  uno: {type: device.instrument, address: sim:uno}
  capture: {type: device.capture, channels: [D3], rate: 100 kHz, samples: 448, pre: 20,
            trigger: {edge: rising, source: D3}}
  later: {type: control.sequence, steps: [{wait_for: armed}, {wait: 3 ms}, {emit: go}]}
  pulse: {type: gpio.pulse, pin: D7, width: 1 ms}
edges:
  - uno.device -> capture.device
  - uno.device -> pulse.device
  - capture.armed -> later.armed
  - later.go -> pulse.trigger
""")
    assert result.ok, result.error
    d3 = seen["capture.D3"][0]
    assert d3.values[:20].max() == 0 and d3.values[20:120].min() == 1  # 1 ms high from the trigger on


def test_a_virtual_stream_ends_at_stop():
    result, seen = run("""
flow: t
nodes:
  la: {type: device.instrument, address: sim:free}
  stream: {type: device.stream, channels: [D15], rate: 100 kHz, duration: 1 s}
  stop: {type: control.timer, delay: 50 ms, count: 1}
edges:
  - la.device -> stream.device
  - stop.tick -> stream.stop
""")
    assert result.ok, result.error
    assert sum(len(block) for block in seen["stream.D15"]) == 5000  # 50 ms, not the whole second
    assert seen["stream.done"][0].data == [5000]


def test_virtual_captures_of_usb_devices_have_the_flows_time():
    result, seen = run("""
flow: t
nodes:
  pico: {type: device.instrument, address: sim:pico}
  wait: {type: control.timer, delay: 200 ms, count: 1}
  capture: {type: device.capture, channels: [GP12], rate: 1 MHz, samples: 1000}
edges:
  - pico.device -> capture.device
  - wait.tick -> capture.arm
""")
    assert result.ok, result.error
    assert seen["capture.capture"][0].start == pytest.approx(0.2005, abs=1e-4)  # (the Pico's latency: 0.5 ms)


def test_a_generator_stopped_during_a_capture_shows_until_its_stop():
    result, seen = run("""
flow: t
nodes:
  r4: {type: device.instrument, address: sim:uno_r4}
  dc: {type: gen.waveform, kind: dc, offset: 3 V, amplitude: 0 V, duration: 5 ms}
  stream: {type: device.stream, channels: [A1], rate: 10 kHz, duration: 10 ms}
edges:
  - r4.device -> dc.device
  - r4.device -> stream.device
""")
    assert result.ok, result.error
    a1 = np.concatenate([block.analog["A1"] for block in seen["stream.capture"]])
    assert a1[:45].mean() == pytest.approx(3.0, abs=0.05) and a1[55:].mean() == pytest.approx(0.0, abs=0.05)


def test_state_mode_sends_the_states():
    result, seen = run("""
flow: t
nodes:
  la: {type: device.instrument, address: sim:free}
  capture: {type: device.capture, channels: [D0, D1, D2, D3], clock: D8, samples: 32, rate: 100 MHz,
            trigger: {pattern: '1x11', source: D0}}
  bit: {type: convert.state_bit, bit: D1}
edges:
  - la.device -> capture.device
  - capture.states -> bit.in
""")
    assert result.ok, result.error
    states = seen["capture.states"][0]
    assert states.channels == ["D0", "D1", "D2", "D3"] and states.values[0] in (13, 15)  # (D1 does not count)
    assert list(np.diff(states.values[1:8].astype(int)) % 16) == [1] * 6  # one count per clock edge
    assert len(seen["bit.out"][0]) == 32


def test_transmit_payloads_and_the_end_of_a_transmission():
    result, seen = run("""
flow: t
nodes:
  uno: {type: device.instrument, address: sim:uno}
  go: {type: control.sequence, steps: [{emit: send}]}
  spi: {type: gen.tx_spi, pins: [D10, D13, D11], clock: 10 kHz, data: [1, 2, 3]}
edges:
  - uno.device -> spi.device
  - go.send -> spi.data
""")
    assert result.ok, result.error  # an event without data sends the 'data' parameter
    done = seen["spi.done"][0]
    assert done.data == [3] and done.times[0] >= 24 / 10_000  # when the 24 bits are through


# ------------------------------------------------------------ simulators
def test_pins_are_released_with_their_pulls_and_generators_on_safe():
    instrument = open_simulated("pico", fast=True)
    gpio = instrument.gpio
    gpio.set_mode("GP13", "input_pullup")
    assert gpio.read("GP13") == 1
    gpio.set_mode("GP13", "input_pulldown")
    assert gpio.read("GP13") == 0
    with pytest.raises(Exception, match="no pin"):
        gpio.read("X1")
    from openscilab.core import waveform as waves

    generator = instrument.simulated_driver.generator
    generator.start("PATTERN", waves.pattern({"GP2": [0, 1] * 8}, rate=1000))
    gpio.safe_all()
    assert not generator.running("PATTERN")


def test_an_rc_low_pass_follows_any_source():
    result, seen = run("""
flow: t
nodes:
  uno: {type: device.instrument, address: sim:uno}
  square: {type: gen.waveform, output: SQUARE, kind: square, frequency: 1 kHz}
  wait: {type: control.timer, delay: 1 s, count: 1}
  a0: {type: gpio.read, pin: A0, analog: true}
edges:
  - uno.device -> square.device
  - uno.device -> a0.device
  - wait.tick -> a0.trigger
""")
    assert result.ok, result.error
    assert seen["a0.value"][0].value == pytest.approx(2.5, abs=0.1)  # half of 5 V, ten time constants later
    assert _low_pass(np.ones(200), 0.01)[-1] == pytest.approx(1.0)


def test_the_simulators_keep_the_limits_of_their_firmware():
    uno = open_simulated("uno", fast=True).simulated_driver
    session = CaptureSession(frequency=100_000, pre_trigger_samples=0, post_trigger_samples=449,
                             trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=0)]
    assert uno.check_capture(session) == CaptureError.BAD_PARAMS  # 448 samples
    session.post_trigger_samples = 448
    assert uno.check_capture(session) == CaptureError.NONE
    assert uno.stream_rate_limit([0]) == 11520  # 115200 baud
    scope = open_simulated("dho924s", fast=True).simulated_driver
    assert scope.acquisition_modes() == ("buffer",)  # no link to stream over
    daq = open_simulated("daq", fast=True).simulated_driver
    assert daq.stream_rate_limit([], 4) == 5000  # one ADC for all inputs: 20 kS/s
    pair = open_simulated("pico*2", fast=True)
    assert not {"STATE_MODE", "GPIO", "GEN_SQUARE", "TX_SPI"} & pair.capture.driver.capabilities()
    assert pair.capture.driver.acquisition_modes() == ("buffer",) and pair.generator is None


def test_a_pattern_trigger_with_dont_care_bits():
    instrument = open_simulated("free", fast=True)
    driver = instrument.simulated_driver
    session = CaptureSession(frequency=10_000_000, pre_trigger_samples=0, post_trigger_samples=100)
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in range(4)]
    session.trigger_type = TriggerType.COMPLEX
    session.trigger_channel, session.trigger_bit_count = 0, 4
    session.trigger_pattern, session.trigger_mask = 0b1101, 0b1101  # D1: don't care
    trigger = driver.find_trigger(session, 0.0, 0.001)
    levels = driver.sample([0, 1, 2, 3], trigger, 10_000_000, 1)
    assert [int(levels[index][0]) for index in (0, 2, 3)] == [1, 1, 1]
    assert session.trigger_description() == "1X11"


def test_decoders_join_the_data_of_the_row_that_has_it():
    result, seen = run("""
flow: t
nodes:
  la: {type: device.instrument, address: sim:free}
  capture: {type: device.capture, channels: [D10, D11, D12], rate: 16 MHz, samples: 64000}
  spi: {type: decode.spi, channels: {clk: D10, mosi: D11, cs: D12}}
edges:
  - la.device -> capture.device
  - capture.capture -> spi.in
""")
    assert result.ok, result.error
    assert seen["spi.text"][0]  # MOSI data, though MISO comes first and is not wired
