"""Generators in flows and in the simulator: a pattern played and captured is identical, the AFG
drives CH1, captures are replayed, protocol blocks, and the sync output over a hub route."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from openscilab.core import waveform as waves
from openscilab.core.compare import compare_sessions
from openscilab.core.hub import Hub
from openscilab.core.instrument import GeneratorFacet, InstrumentError
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import follow_routes, open_simulated
from openscilab.lab import yaml_io
from openscilab.lab.engine import Engine

SDL = {"D0": "l5;h3;l2;h10;l4;h6;", "D1": "h7;l13;h10;", "D2": "{l1,h1,}15;", "D7": "h1;l29;"}


def run(text: str) -> dict:
    flow = yaml_io.loads(text)
    seen: dict = {}
    engine = Engine(flow, mode="virtual")
    engine.subscribe(lambda event: event.kind == "value" and seen.setdefault(f"{event.node}.{event.port}", []).append(
        event.value))
    result = engine.run(timeout=30)
    assert result.ok, result.error
    return seen


def reference(pattern: waves.Waveform, count: int) -> CaptureSession:
    session = CaptureSession(frequency=int(pattern.rate), pre_trigger_samples=0, post_trigger_samples=count)
    levels = pattern.samples(pattern.rate, count)
    session.capture_channels = [AnalyzerChannel(channel_number=int(pin[1:]), channel_name=pin, samples=levels[pin])
                                for pin in pattern.tracks]
    return session


def test_an_sdl_pattern_played_and_captured_is_identical():
    seen = run("""
flow: Pattern
nodes:
  sim: {type: device.instrument, address: "sim:free"}
  gen: {type: gen.pattern, rate: 1 MHz, tracks: {D0: "l5;h3;l2;h10;l4;h6;", D1: "h7;l13;h10;",
        D2: "{l1,h1,}15;", D7: "h1;l29;"}}
  cap: {type: device.capture, channels: [D0, D1, D2, D7], rate: 1 MHz, samples: 90,
        trigger: {edge: rising, source: D7}}
edges:
  - sim.device -> gen.device
  - sim.device -> cap.device
  - gen.sync -> cap.arm
""")
    capture = seen["cap.capture"][-1]
    pattern = waves.pattern_from_sdl(SDL, "1 MHz")
    played = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=90)
    played.capture_channels = [AnalyzerChannel(channel_number=int(name[1:]), channel_name=name,
                                               samples=capture.digital[name]) for name in ("D0", "D1", "D2", "D7")]
    result = compare_sessions(reference(pattern, 90), played)
    assert result.compared_samples == 90 and result.differing_samples == 0


def test_the_afg_drives_ch1():
    seen = run("""
flow: AFG
nodes:
  scope: {type: device.instrument, address: "sim:dho924s"}
  gen: {type: gen.waveform, kind: square, frequency: 10 kHz, amplitude: 1.5 V, offset: 0.5 V}
  cap: {type: device.capture, channels: [CH1], rate: 10 MHz, samples: 20000}
edges:
  - scope.device -> gen.device
  - scope.device -> cap.device
  - gen.sync -> cap.arm
""")
    volts = seen["cap.capture"][-1].analog["CH1"]
    assert volts.max() == pytest.approx(2.0, abs=0.05) and volts.min() == pytest.approx(-1.0, abs=0.05)
    rising = np.flatnonzero((volts[:-1] < 0.5) & (volts[1:] >= 0.5))
    assert np.diff(rising).mean() == pytest.approx(1000, abs=2)  # 10 kHz at 10 MSa/s


def test_limits_are_refused():
    instrument = open_simulated("dho924s", fast=True)
    generator = instrument.facet(GeneratorFacet)
    with pytest.raises(InstrumentError, match="above"):
        generator.start("GI", waves.standard(waves.SINE, "50 MHz", 1))
    with pytest.raises(InstrumentError, match="pattern"):
        open_simulated("free").facet(GeneratorFacet).start("PATTERN", waves.standard(waves.SINE, 10, 1))


def test_stopping_brings_back_the_signals():
    class Clock:
        time = 0.0

    clock = Clock()
    instrument = open_simulated("free", clock=lambda: clock.time, fast=True)
    model = instrument.simulated_driver
    before = model.circuit.digital("D0", 0.0, 1e6, 100).copy()
    generator = instrument.facet(GeneratorFacet)
    generator.start("PATTERN", waves.pattern({"D0": np.ones(10)}))
    assert model.circuit.digital("D0", 0.0, 1e6, 100).min() == 1
    generator.stop("PATTERN")
    assert np.array_equal(model.circuit.digital("D0", 0.0, 1e6, 100), before)


def test_replaying_a_capture():
    seen = run("""
flow: Replay
nodes:
  sim: {type: device.instrument, address: "sim:free"}
  first:  {type: device.capture, channels: [D9], rate: 1 MHz, samples: 3000}
  replay: {type: gen.replay, pins: {D9: D3}, repeat: 0}
  second: {type: device.capture, channels: [D3], rate: 1 MHz, samples: 3000}
edges:
  - sim.device -> first.device
  - sim.device -> replay.device
  - sim.device -> second.device
  - first.capture -> replay.capture
  - replay.sync -> second.arm
""")
    original = seen["first.capture"][-1].digital["D9"]
    replayed = seen["second.capture"][-1].digital["D3"]
    # the replay repeats the capture: the second one is a shifted copy of it
    doubled = np.concatenate([original, original])
    assert any(np.array_equal(doubled[shift:shift + 3000], replayed) for shift in range(3000))


def test_sending_uart():
    seen = run("""
flow: UART
nodes:
  sim: {type: device.instrument, address: "sim:free"}
  tx:  {type: gen.tx_uart, pin: D4, baud: 100 kHz, rate: 1 MHz, data: "Hi"}
  cap: {type: device.capture, channels: [D4], rate: 1 MHz, samples: 300,
        trigger: {edge: falling, source: D4}}
edges:
  - sim.device -> tx.device
  - sim.device -> cap.device
  - tx.sync -> cap.arm
""")
    levels = seen["cap.capture"][-1].digital["D4"]
    bits = levels[5:200:10]
    assert list(bits[:10]) == [0] + [0, 0, 0, 1, 0, 0, 1, 0] + [1]  # "H" = 0x48, LSB first
    assert seen["tx.done"]


def test_sync_over_a_hub_route_triggers_another_instrument():
    hub = Hub()
    scope, logic = open_simulated("dho924s", name="scope"), open_simulated("free", name="logic")
    hub.add(scope)
    hub.add(logic)
    assert "SYNC" in scope.trigger_outputs and "TRIG IN" in logic.trigger_inputs
    stop = follow_routes(hub)
    try:
        hub.add_route("scope", "SYNC", "logic", "TRIG IN")
        scope.facet(GeneratorFacet).start("GI", waves.standard(waves.SINE, "2 kHz", 1))
        session = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=2000,
                                 trigger_type=TriggerType.EDGE, trigger_channel=15)
        session.capture_channels = [AnalyzerChannel(channel_number=15)]
        done = threading.Event()
        assert logic.capture.driver.start_capture(session, lambda args: done.set()).name == "NONE"
        assert done.wait(10)
        levels = session.capture_channels[0].samples
        assert levels[0] == 1 and int(levels.sum()) == pytest.approx(1000, abs=3)  # 2 kHz square on D15
    finally:
        stop()
        hub.close_all()


def test_a_waveform_file_on_an_output(tmp_path):
    path = tmp_path / "wave.wave.yaml"
    waves.save(waves.standard(waves.TRIANGLE, "5 kHz", "1 V"), str(path))
    seen = run(f"""
flow: File
nodes:
  scope: {{type: device.instrument, address: "sim:dho924s"}}
  out: {{type: gen.output, file: "{path}", duration: 1 ms}}
  cap: {{type: device.capture, channels: [CH1], rate: 1 MHz, samples: 400}}
edges:
  - scope.device -> out.device
  - scope.device -> cap.device
  - out.sync -> cap.arm
""")
    volts = seen["cap.capture"][-1].analog["CH1"]
    assert volts.max() == pytest.approx(1.0, abs=0.05) and volts.min() == pytest.approx(-1.0, abs=0.05)
    assert seen["out.done"]


def test_analog_channels_in_the_replay_session():
    session = CaptureSession(frequency=1000)
    session.analog_channels = [AnalogChannel.from_volts(np.linspace(0, 1, 100), channel_name="CH1")]
    wave = waves.from_analog(session.analog_channels[0])
    assert wave.voltage_range() == pytest.approx((0.0, 1.0), abs=1e-3)
