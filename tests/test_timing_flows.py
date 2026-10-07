"""Time in flows: a sync signal from openSciLab aligns a USB instrument with a reference, a loopback
measures its latency, a remote DAQ records the sync signal in its blocks (with its own sample clock
or a shared one), a device clock that follows a time scale, and the DAQ measuring its latency."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import numpy as np
import pytest

from openscilab.core import preferences, timing
from openscilab.lab import yaml_io
from openscilab.lab.engine import Engine
from openscilab.lab.project import Project


def project(tmp_path, wiring: str) -> Project:
    (tmp_path / "project.yaml").write_text("project: t\nsimulation:\n  wiring:\n" + wiring)
    return Project.open(str(tmp_path))


def run(text: str, tmp_path, wiring: str, timeout: float = 40.0):
    values: dict[str, list] = {}
    engine = Engine(yaml_io.loads(text), mode="real", project=project(tmp_path, wiring))
    engine.subscribe(lambda event: event.kind == "value" and values.setdefault(f"{event.node}.{event.port}", [])
                     .append(event.value))
    result = engine.run(timeout=timeout)
    assert result.ok, result.error
    return engine, values


def edge_times(blocks) -> np.ndarray:
    found = [timing.edges(block.values, block.time.start, block.time.rate)[0] for block in blocks]
    return np.concatenate(found) if found else np.array([])


# ------------------------------------------------------------------ local
ALIGN = """
flow: Align
nodes:
  uno: {type: device.instrument, address: "sim:uno"}
  la: {type: device.instrument, address: "sim:free"}
  pico: {type: device.instrument, address: "sim:pico"}
  sync: {type: timing.sync, pin: D7, duration: 1.4 s}
  ref: {type: device.stream, channels: [D5], rate: 100 kHz, duration: 1.6 s}
  rec: {type: device.stream, channels: [GP15], rate: 100 kHz, duration: 1.6 s}
  align: {type: timing.align}
edges:
  - uno.device -> sync.device
  - la.device -> ref.device
  - pico.device -> rec.device
  - pico.device -> align.device
  - ref.D5 -> align.reference
  - rec.GP15 -> align.signal
  - rec.GP15 -> align.in
"""
ALIGN_WIRING = "    la: [{from: \"uno:D7\", to: D5}]\n    pico: [{from: \"uno:D7\", to: GP15}]\n"


def test_a_sync_signal_aligns_a_usb_instrument_with_a_reference(tmp_path):
    engine, values = run(ALIGN, tmp_path, ALIGN_WIRING)
    pico = engine.devices["pico"]
    state = timing.timing_of(pico).state()
    assert state.method == timing.METHOD_SIGNAL and state.reference == "la"
    assert values["align.uncertainty"][-1].value < 50e-6
    reference = edge_times(values["ref.D5"])
    raw = edge_times(values["rec.GP15"])
    aligned = edge_times(values["align.out"])
    # the recordings of the same edges meet after the alignment (a sample or two: 10-20 µs)
    nearest = lambda times: np.array([np.min(np.abs(reference - t)) for t in times])
    assert np.median(nearest(aligned)) < 20e-6
    assert np.median(nearest(raw)) > np.median(nearest(aligned))  # (USB: estimated, ms off)


CALIBRATE = """
flow: Calibrate
nodes:
  pico: {type: device.instrument, address: "sim:pico"}
  cal: {type: timing.calibrate, pin: GP16, channel: GP17, rate: 100 kHz, repeats: 8}
edges:
  - pico.device -> cal.device
"""


def test_a_loopback_measures_the_latency_and_streams_use_it(tmp_path):
    engine, values = run(CALIBRATE, tmp_path, "    pico: [{from: GP16, to: GP17}]\n")
    latency = values["cal.latency"][-1].value
    uncertainty = values["cal.uncertainty"][-1].value
    assert 0.0002 < latency < 0.003 and uncertainty <= latency + 1e-9
    pico = engine.devices["pico"]
    key = timing.calibration_key(pico, "stream", 100_000.0)
    assert timing.stored_latency(key).value == pytest.approx(latency)
    # a stream at that rate is placed with it: within the uncertainty of the truth
    flow = """
flow: S
nodes:
  pico: {type: device.instrument, address: "sim:pico"}
  rec: {type: device.stream, channels: [GP12], rate: 100 kHz, duration: 0.5 s}
edges:
  - pico.device -> rec.device
"""
    engine, _values = run(flow, tmp_path, "    pico: [{from: GP16, to: GP17}]\n")
    pico = engine.devices["pico"]
    acquisition = timing.timing_of(pico).current
    model = pico.simulated_driver
    truth = model.last_stream_start - (model.clock() - time.monotonic())
    assert acquisition.state().method == timing.METHOD_LATENCY
    assert abs(acquisition.time_of(0) - truth) <= uncertainty + 0.0006  # (+ the frame the envelope may keep)


def test_a_loopback_in_a_fast_capture_switches_within_it(tmp_path):
    flow = CALIBRATE.replace("rate: 100 kHz, repeats: 8", "rate: 10 MHz, mode: capture, store: false")
    _engine, values = run(flow, tmp_path, "    pico: [{from: GP16, to: GP17}]\n")
    assert values["cal.latency"][-1].value > 0  # (10 ms of samples: four switches, 2 ms apart)
    too_fast = CALIBRATE.replace("rate: 100 kHz", "rate: 100 MHz, mode: capture")
    result = Engine(yaml_io.loads(too_fast), mode="real",
                    project=project(tmp_path, "    pico: [{from: GP16, to: GP17}]\n")).run(timeout=40)
    assert not result.ok and "too short to switch the output" in str(result.error)


def test_a_simulator_without_usb_knows_its_time(tmp_path):
    flow = """
flow: S
nodes:
  la: {type: device.instrument, address: "sim:free"}
  rec: {type: device.stream, channels: [D1], rate: 100 kHz, duration: 0.2 s}
edges:
  - la.device -> rec.device
"""
    engine, _values = run(flow, tmp_path, "    la: []\n")
    assert timing.timing_of(engine.devices["la"]).state().method == timing.METHOD_SIMULATED


# ----------------------------------------------------------------- remote
DAQ = """
flow: Daq
nodes:
  uno: {type: device.instrument, address: "sim:uno"}
  la: {type: device.instrument, address: "sim:free"}
  daq: {type: device.instrument, address: "%s"}
  sync: {type: timing.sync, pin: D7, duration: 1.8 s}
  ref: {type: device.stream, channels: [D5], rate: 100 kHz, duration: 2 s}
  align: {type: remote.sync, sync: sync}
edges:
  - uno.device -> sync.device
  - la.device -> ref.device
  - daq.device -> align.device
  - ref.D5 -> align.signal
"""
DAQ_WIRING = "    la: [{from: \"uno:D7\", to: D5}]\n    daq: [{from: \"uno:D7\", to: SYNC_IN}]\n"


@pytest.mark.parametrize("address", ["remote-sim:daq", "remote-sim:daq?shared_clock=1"])
def test_a_remote_daq_records_openscilabs_sync_signal(tmp_path, address):
    _engine, values = run(DAQ % address, tmp_path, DAQ_WIRING)
    scatter = values["align.uncertainty"][-1].value
    edges = values["align.edges"][-1].value
    # the DAQ samples at 10 kHz: its edges are at most a sample (100 µs) off, the reference 10 µs
    assert edges >= 10 and scatter < 60e-6


def test_a_device_clock_that_follows_a_time_scale(monkeypatch):
    from openscilab.driver.remote.simulated import open_simulated_remote

    monkeypatch.setitem(preferences._load(), "timing.host_clock", "ptp")
    instrument = open_simulated_remote("remote-sim:daq?timescale=utc")
    try:
        connection = instrument.remote.connection
        state = connection.clock.state(time.monotonic())
        assert connection.timescale == "utc" and state.method == "timescale" and state.timescale == "utc"
        assert state.uncertainty == pytest.approx(timing.HOST_ACCURACY["ptp"] + 2e-5)
        now = time.monotonic()
        device_now = instrument.remote.simulated.device.now()
        assert connection.clock.to_local(device_now) == pytest.approx(now, abs=0.002)
        details = dict(dict(instrument.details())["Clock"])
        assert details["Method"].startswith("time scale UTC")
    finally:
        instrument.remote.close()
    preferences._load()["timing.host_clock"] = "free"
    instrument = open_simulated_remote("remote-sim:daq?timescale=utc")
    try:
        assert instrument.remote.connection.clock.state().method in ("network", "none")  # this computer: free
    finally:
        instrument.remote.close()


def test_the_daq_measures_its_latency_with_its_loopback():
    from openscilab.driver.remote.simulated import open_simulated_remote

    instrument = open_simulated_remote("remote-sim:daq")
    try:
        remote = instrument.remote
        received = []
        stop = remote.listen("ai0", received.append)
        time.sleep(0.3)
        assert received and received[-1].method == "bounds"
        result = remote.call("calibrate_latency", {"repeats": 6}, timeout=20)
        # the emulated USB: an input latency of 1.2 ms (+ up to a frame), a command latency of 0.6-0.9 ms
        assert 0.0003 < result["latency"] < 0.003 and result["uncertainty"] > 0
        received.clear()
        time.sleep(0.2)
        assert received[-1].method == "latency" and received[-1].uncertainty == pytest.approx(result["uncertainty"])
        stop()
        assert "measured latency" in dict(dict(instrument.details())["Inputs"])["ai0"]
    finally:
        instrument.remote.close()


def test_the_ni_daq_template_runs_without_the_hardware(tmp_path):
    import subprocess
    import sys

    from openscilab.driver.remote.server import RemoteServer

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server = RemoteServer(port=0, host="127.0.0.1", token="secret").start()
    process = subprocess.Popen([sys.executable, os.path.join(root, "examples", "remote", "ni_daq.py"),
                                "--server", f"127.0.0.1:{server.port}", "--token", "secret"],
                               cwd=str(tmp_path), env={**os.environ, "PYTHONPATH": root},
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        connection = server.wait_for("ni-daq", 20)
        assert set(connection.inputs()) == {"ai0", "sync", "loop"} and "sync" in connection.syncs()
        received = []
        stop = connection.listen("ai0", received.append)
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)
        assert received and received[-1].method == "bounds"
        result = connection.call("calibrate_latency", {"repeats": 5}, timeout=20)
        assert 0 < result["latency"] < 0.01
        assert os.path.exists(tmp_path / "ni_daq_latency.json")
        stop()
    finally:
        process.terminate()
        process.wait(5)
        server.stop()


def test_the_time_settings_and_the_card(qapp, tmp_path):
    from openscilab.ui.dialogs.preferences_dialog import PreferencesDialog
    from openscilab_device.timing import Latency

    timing.store_latency("Pico|pico:x|stream|100000", Latency(0.0011, 0.0002, "loopback"))
    dialog = PreferencesDialog()
    assert "Pico (stream, 100000 Hz): 1.100 ms" in dialog.latencies.text()
    dialog.host_clock.setCurrentIndex(dialog.host_clock.findData("ptp"))
    dialog.host_accuracy.setValue(0.02)
    chosen = dialog.chosen()
    assert chosen["timing.host_clock"] == "ptp" and chosen["timing.host_accuracy"] == pytest.approx(2e-5)
    dialog._forget_latencies()
    assert dialog.latencies.text() == "none" and timing.stored_latency("Pico|pico:x|stream|100000") is None
    flow = """
flow: S
nodes:
  pico: {type: device.instrument, address: "sim:pico"}
  rec: {type: device.stream, channels: [GP12], rate: 100 kHz, duration: 0.2 s}
edges:
  - pico.device -> rec.device
"""
    engine, _values = run(flow, tmp_path, "    pico: []\n")
    rows = dict(dict(engine.devices["pico"].details())["Timing"])
    assert rows["Method"] == "between start command and arrival" and rows["Accuracy"].startswith("± ")
