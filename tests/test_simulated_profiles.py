"""The simulator profiles and what makes them complete: every profile loads and opens with its
facets, every source type, wiring from the project, fault injection (menu and API), determinism,
the device list."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from openscilab.core.instrument import (
    AnalogInFacet,
    AnalogOutFacet,
    GeneratorFacet,
    GpioFacet,
    InstrumentError,
    InstrumentStatus,
    MonitorFacet,
)
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import available_profiles, load_profile, open_simulated
from openscilab.driver.simulated.circuit import SOURCE_TYPES, make_source

PROFILES = ["dho924s", "free", "pico", "uno", "uno_r4"]
#: facets every profile must have
FACETS = {
    "free": {GeneratorFacet},
    "dho924s": {AnalogInFacet, GeneratorFacet},
    "uno": {GpioFacet, MonitorFacet, AnalogInFacet},
    "uno_r4": {GpioFacet, MonitorFacet, AnalogInFacet, AnalogOutFacet, GeneratorFacet},
    "pico": {GpioFacet, MonitorFacet, AnalogInFacet, GeneratorFacet},
}


def capture(instrument, session: CaptureSession, timeout: float = 10.0):
    done = threading.Event()
    results = []
    error = instrument.capture.driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert error.name == "NONE", error
    assert done.wait(timeout)
    return results[0]


def test_every_profile_is_there():
    assert set(PROFILES) <= set(available_profiles())


@pytest.mark.parametrize("name", PROFILES)
def test_a_profile_opens_with_its_facets_and_limits(name):
    profile = load_profile(name)
    instrument = open_simulated(name, fast=True)
    assert instrument.kind == profile["title"] and instrument.status == InstrumentStatus.SIMULATED
    for facet in FACETS[name]:
        assert instrument.facet(facet) is not None, facet
    driver = instrument.capture.driver
    assert driver.max_frequency == profile["max_rate"]
    assert driver.channel_count == len(profile["digital"])
    # a short capture of every channel works and stays within the limits of the profile
    session = CaptureSession(frequency=min(profile["max_rate"], 1_000_000), pre_trigger_samples=0,
                             post_trigger_samples=200, trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in range(driver.channel_count)]
    if profile.get("buffer_analog", True):  # (an Arduino's buffer captures read the pins only)
        session.analog_channels = [AnalogChannel(channel_number=index) for index in range(len(profile["analog"]))]
    result = capture(instrument, session)
    assert result.success and session.sample_count() == 200
    too_long = CaptureSession(frequency=1000, pre_trigger_samples=0,
                              post_trigger_samples=driver.memory_depth(1, 0) + 1, trigger_type=TriggerType.IMMEDIATE)
    too_long.capture_channels = [AnalyzerChannel(channel_number=0)]
    assert driver.start_capture(too_long).name == "BAD_PARAMS"


def test_the_pico_depth_follows_the_channels():
    driver = open_simulated("pico", fast=True).capture.driver
    assert [driver.memory_depth(count, 0) for count in (4, 8, 12, 24)] == [131072, 131072, 65536, 32768]


def test_the_uno_r4_dac_is_wired_to_a1():
    class Clock:
        time = 0.0

    clock = Clock()
    instrument = open_simulated("uno_r4", clock=lambda: clock.time, fast=True)
    instrument.facet(AnalogOutFacet).set_voltage("A0", 2.5)
    clock.time = 0.01
    assert instrument.facet(AnalogInFacet).read(["A1"])["A1"] == pytest.approx(2.5, abs=0.01)
    assert {pin.name: pin for pin in instrument.pins()}["D0"].usable  # native USB: D0/D1 are free


@pytest.mark.parametrize("kind", sorted(SOURCE_TYPES))
def test_every_source_type(kind):
    options = {"button": {"presses": [[0.001, 0.002]]}, "pulse": {"period": "1 ms", "width": "250 us"},
               "uart": {"text": "A"}, "counter": {"bit": 1},
               "file": {"path": os.path.join(os.path.dirname(__file__), "..", "examples", "demo.lac")}}.get(kind, {})
    source = make_source({"type": kind, **options})
    digital = source.digital(0.0, 1e6, 5000)
    analog = source.analog(0.0, 1e6, 5000)
    assert digital.dtype == np.uint8 and len(digital) == 5000 and set(np.unique(digital)) <= {0, 1}
    assert analog.dtype == np.float64 and np.all(np.isfinite(analog))
    if kind == "pulse":
        assert digital[:1000].sum() == 250
    # the same interval gives the same samples
    assert np.array_equal(source.analog(0.0, 1e6, 5000), analog)


def test_a_sum_and_analog_over_a_threshold():
    source = make_source({"type": "sum", "parts": [{"type": "sine", "frequency": "1 kHz", "amplitude": "1 V"},
                                                    {"type": "constant", "level": 2.0}]})
    values = source.analog(0.0, 1e5, 100)
    assert values.max() == pytest.approx(3.0, abs=0.01) and values.min() == pytest.approx(1.0, abs=0.01)
    # the logic level of an analog net: above half of 3.3 V
    assert np.array_equal(source.digital(0.0, 1e5, 100), (values >= 1.65).astype(np.uint8))


def test_wiring_from_the_project(tmp_path):
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine
    from openscilab.lab.project import Project

    (tmp_path / "project.yaml").write_text(
        "project: Wired\ndevices: {sim: 'sim:free'}\n"
        "simulation:\n  wiring:\n    sim: [{from: D15, to: D3}]\n")
    project = Project.open(str(tmp_path))
    flow = project.complete(yaml_io.loads("""
flow: Wired
nodes:
  sim: {type: device.instrument}
  cap: {type: device.capture, channels: [D3, D15], rate: 100 kHz, samples: 400}
edges:
  - sim.device -> cap.device
"""))
    seen = {}
    engine = Engine(flow, mode="virtual", project=project)
    engine.subscribe(lambda event: event.kind == "value" and event.port == "capture" and seen.update(c=event.value))
    assert engine.run(timeout=20).ok
    assert np.array_equal(seen["c"].digital["D3"], seen["c"].digital["D15"])  # D3 follows D15 now


def test_faults_through_the_api():
    from openscilab import api

    with api.open("sim:free") as device:
        device.inject("delay", 0.05)
        device.inject("overflow")
        assert device.driver.inner.force_overflow
        with pytest.raises(ValueError):
            device.inject("melt")
    uno = api.instrument("sim:uno")
    uno.simulated_driver.inject("disconnect")
    with pytest.raises(InstrumentError):
        uno.gpio.write("D7", 1)
    uno.simulated_driver.inject("restart")
    uno.gpio.write("D7", 1)


def test_an_overflow_ends_the_stream_early():
    instrument = open_simulated("free", fast=True)
    instrument.simulated_driver.inject("overflow")
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=200_000,
                             trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream")
    session.capture_channels = [AnalyzerChannel(channel_number=0)]
    result = capture(instrument, session)
    assert result.error and "overflow" in result.error.lower()


def test_the_same_seed_gives_the_same_samples():
    def run(seed: int) -> np.ndarray:
        # fast mode on a fixed clock (a flow's virtual time): the same interval of the same signals
        instrument = open_simulated("dho924s", clock=lambda: 0.25, fast=True, seed=seed)
        session = CaptureSession(frequency=10_000_000, pre_trigger_samples=0, post_trigger_samples=2000,
                                 trigger_type=TriggerType.IMMEDIATE)
        session.analog_channels = [AnalogChannel(channel_number=0)]
        capture(instrument, session)
        return session.analog_channels[0].raw.copy()

    first, again, other = run(3), run(3), run(4)
    assert np.array_equal(first, again)
    assert not np.array_equal(first, other)  # the noise depends on the seed


def test_a_flow_twice_gives_the_same_result():
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    text = """
flow: Twice
nodes:
  scope: {type: device.instrument, address: "sim:dho924s"}
  cap: {type: device.capture, channels: [CH1, D9], rate: 10 MHz, samples: 5000}
edges:
  - scope.device -> cap.device
"""
    results = []
    for _ in range(2):
        seen = {}
        engine = Engine(yaml_io.loads(text), mode="virtual", seed=7)
        engine.subscribe(lambda event: event.kind == "value" and event.port == "capture" and seen.update(c=event.value))
        assert engine.run(timeout=20).ok
        results.append(seen["c"])
    assert np.array_equal(results[0].analog["CH1"], results[1].analog["CH1"])
    assert np.array_equal(results[0].digital["D9"], results[1].digital["D9"])


# ---------------------------------------------------------------- the shell
def test_the_device_list_offers_the_simulators_and_the_fault_menu(shell):
    from openscilab.ui.devices.simulated import FAULTS, fill_simulation_menu

    section = shell.devices_section
    section.refresh()
    labels = [section.list.item(index).text() for index in range(section.list.count())]
    group = next(label for label in labels if label.startswith("Simulators ("))
    assert "Simulation: Pico" not in " ".join(labels)  # the group starts closed
    group_item = section.list.item(labels.index(group))
    section.list.itemClicked.emit(group_item)  # one click opens it
    labels = [section.list.item(index).text().strip() for index in range(section.list.count())]
    assert {"Arduino Uno", "Uno R4", "Pico", "DHO924S", "free"} <= set(labels)
    entry = next(section.list.item(index).data(0x0100) for index in range(section.list.count())
                 if section.list.item(index).text().strip() == "Arduino Uno")
    instrument = shell.connect_entry(entry)
    assert instrument is not None and instrument in shell.hub
    assert instrument.simulated_driver.clock() == pytest.approx(shell.hub.now(), abs=0.05)  # the hub's clock
    fill_simulation_menu(shell.simulation_menu, shell.hub)
    submenu = shell.simulation_menu.actions()[0].menu()
    assert [action.text() for action in submenu.actions()] == [title for title, _fault, _value in FAULTS]
    submenu.actions()[0].trigger()  # disconnect
    assert instrument.status == InstrumentStatus.DISCONNECTED
    submenu.actions()[1].trigger()  # reconnect
    assert instrument.status == InstrumentStatus.SIMULATED
