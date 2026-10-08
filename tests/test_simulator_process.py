"""Simulators of USB devices in a device process of their own (docs/roadmap 4c): the device list
opens them there, the device card reaches what they simulate through their simulation facet, and
the simulators that know the time of their samples stay in the application."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import preferences
from openscilab.driver import process
from openscilab.driver.simulated import open_simulated
from openscilab.driver.simulated.stored import open_stored, remember_circuit, stored_circuit


def wait(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    return condition()


def test_which_simulators_run_in_a_device_process():
    assert preferences.DEFAULTS["devices.simulator_process"] is False  # (the tests switch it off)
    preferences._load()["devices.simulator_process"] = True
    # those that emulate a USB link, as the device they simulate
    assert process.simulator_wanted("sim:pico") and process.simulator_wanted("sim:daq")
    assert process.simulator_wanted("sim:pico*2#2")
    # those that know the time of their samples, and what is no simulator, stay here
    assert not process.simulator_wanted("sim:uno") and not process.simulator_wanted("sim:dho924s")
    assert not process.simulator_wanted("pico:/dev/cu.test") and not process.simulator_wanted("sim:nothing")
    # the USB link it was given counts
    remember_circuit("sim:uno", usb={"latency": 0.002})
    remember_circuit("sim:pico", usb=False)
    assert process.simulator_wanted("sim:uno") and not process.simulator_wanted("sim:pico")
    preferences._load()["devices.simulator_process"] = False
    assert not process.simulator_wanted("sim:uno")


def test_a_simulator_opens_as_it_was_left():
    remember_circuit("sim:pico", wiring=[{"from": "GP16", "to": "GP17"}], drift=50.0)
    pico = open_stored("sim:pico")
    simulation = pico.simulation
    assert simulation.wiring() == [{"from": "GP16", "to": "GP17"}] and ("GP16", "GP17") in simulation.wires()
    assert simulation.drift() == pytest.approx(50e-6)
    assert not simulation.knows_time() and simulation.usb()["latency"] == pytest.approx(0.0012)
    with pytest.raises(ValueError, match="loop"):
        simulation.set_wiring([{"from": "GP16", "to": "GP17"}, {"from": "GP17", "to": "GP16"}])
    config, names = simulation.apply_signals({"scenario": "uart"})
    assert config["scenario"] == "uart" and simulation.signals()["scenario"] == "uart" and names
    assert next(key for key, _reason in simulation.scenarios()) == "default"
    daq = open_simulated("daq").simulation
    assert daq.profile_wiring()[0] == {"from": "P0.0", "to": "P0.1"} and daq.wiring() == []
    assert "P0.0" in daq.pin_names() and daq.channel_names()[0] == "P0.0" and daq.analog_channel_names()[0] == "AI0"
    assert "wired to P0.0" in daq.circuit()["P0.1"]


def test_the_device_list_runs_a_usb_simulator_in_a_device_process(shell):
    from openscilab.core.device_summary import summarize
    from openscilab.ui.devices import DeviceEntry
    from openscilab.ui.devices.simulated import inject

    preferences._load()["devices.simulator_process"] = True
    instrument = shell.connect_entry(DeviceEntry("sim", "profile", "pico", "Simulation: Pico", simulated=True))
    try:
        assert instrument in shell.hub and instrument.process.alive and instrument.process.pid != os.getpid()
        assert instrument.is_simulated and instrument.simulation is not None
        assert instrument.capture.driver.is_simulator  # (its first capture needs no trigger, as here)
        card = shell.open_device_card(instrument)
        panel = card.signals_panel
        assert panel is not None and panel.usb_box.isChecked()  # (it emulates USB)
        # what it simulates, through its pipe
        panel.scenario_box.setCurrentIndex(panel.scenario_box.findData("uart"))
        assert panel.apply() and instrument.simulation.signals()["scenario"] == "uart"
        # the latency over its emulated USB, wired on the Timing tab
        card.loop_output.setCurrentText("GP16")
        card.loop_input.setCurrentText("GP17")
        assert card.wire_loopback() and instrument.simulation.wiring() == [{"from": "GP16", "to": "GP17"}]
        assert stored_circuit("sim:pico")["wiring"] == [{"from": "GP16", "to": "GP17"}]
        assert panel.wire_table.rowCount() == 1
        assert card.measure_latency(), card.banner.label.text()
        # what it does arrives as events from its process
        assert wait(lambda: card.event_list.count() > 0)
        inject(shell.hub, instrument, "delay", 0.1)
        assert wait(lambda: any("fault: delay" in card.event_list.item(row).text()
                                for row in range(card.event_list.count())))
        # the device list shows the processor load of its process
        assert wait(lambda: instrument.process.cpu_load is not None, 5.0)
        assert summarize(instrument).process_load is not None
    finally:
        shell.disconnect_instrument(instrument, ask=False)
    assert wait(lambda: not instrument.process.alive)


def test_a_simulator_that_knows_its_time_stays_in_the_application(shell):
    from openscilab.ui.devices import DeviceEntry

    preferences._load()["devices.simulator_process"] = True
    uno = shell.connect_entry(DeviceEntry("sim", "profile", "uno", "Simulation: Arduino Uno", simulated=True))
    try:
        assert getattr(uno, "process", None) is None and uno.simulated_driver is not None
        assert shell.open_device_card(uno).signals_panel is not None
    finally:
        shell.disconnect_instrument(uno, ask=False)


# ------------------------------------------------- wires across processes (roadmap 4d)
def in_process(address: str):
    return process.open_instrument(address, factory=process.SIMULATOR_FACTORY)


def level_becomes(read, level: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read() == level:
            return True
        time.sleep(0.01)
    return False


def test_a_net_is_wired_between_two_device_processes():
    from openscilab.driver.simulated import connect_nets

    pico, daq = in_process("sim:pico"), in_process("sim:daq")
    try:
        assert pico.process.pid != daq.process.pid
        undo = connect_nets(pico, "GP16", daq, "P0.7")
        assert "wired from Simulation: Pico GP16" in daq.simulation.circuit()["P0.7"]
        pico.gpio.write("GP16", 1)  # (in the Pico's process; the DAQ reads it in its own)
        assert level_becomes(lambda: daq.gpio.read("P0.7"), 1)
        pico.gpio.write("GP16", 0)
        assert level_becomes(lambda: daq.gpio.read("P0.7"), 0)
        # the wire stays when the DAQ simulates something else
        daq.simulation.apply_signals({"scenario": "counter"})
        pico.gpio.write("GP16", 1)
        assert level_becomes(lambda: daq.gpio.read("P0.7"), 1)
        undo()
        assert "wired" not in daq.simulation.circuit().get("P0.7", "")
        assert level_becomes(lambda: daq.gpio.read("P0.7"), 0)
    finally:
        pico.close()
        daq.close()


def test_a_net_is_wired_between_the_application_and_a_device_process():
    from openscilab.driver.simulated import connect_nets

    uno, daq = open_stored("sim:uno"), in_process("sim:daq")
    try:
        connect_nets(uno, "D7", daq, "P0.6")  # application -> device process
        connect_nets(daq, "P0.0", uno, "D2")  # device process -> application
        uno.gpio.write("D7", 1)
        assert level_becomes(lambda: daq.gpio.read("P0.6"), 1)
        daq.gpio.write("P0.0", 1)
        assert level_becomes(lambda: uno.gpio.read("D2"), 1)
        # the DAQ's process ends: the wire reads low and says why, nothing fails
        daq.close()
        assert level_becomes(lambda: uno.gpio.read("D2"), 0)
        assert "gone" in uno.simulated_driver.circuit.sources["D2"].describe()
    finally:
        uno.close()
        daq.close()


def test_a_trigger_route_between_two_device_processes():
    import threading

    from openscilab.core import waveform as waves
    from openscilab.core.hub import Hub
    from openscilab.core.instrument import GeneratorFacet
    from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
    from openscilab.driver.simulated import follow_routes

    hub = Hub()
    scope, logic = in_process("sim:dho924s"), in_process("sim:free")
    scope.name, logic.name = "scope", "logic"
    hub.add(scope)
    hub.add(logic)
    stop = follow_routes(hub)
    try:
        hub.add_route("scope", "SYNC", "logic", "TRIG IN")
        assert "wired from scope SYNC" in logic.simulation.circuit()["D15"]
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


@pytest.mark.parametrize("where", ["device process", "application"])
def test_the_start_example_aligns_simulators_of_the_device_list(where):
    import numpy as np

    from openscilab.core.hub import Hub
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine
    from openscilab.lab.project import Project

    root = os.path.join(os.path.dirname(__file__), "..", "examples", "library", "00-start",
                        "04-synchronized-instruments")
    hub = Hub()
    if where == "device process":
        logic, daq = in_process("sim:pico"), in_process("sim:daq")
    else:
        logic, daq = open_stored("sim:pico", clock=hub.now), open_stored("sim:daq", clock=hub.now)
    logic.name, daq.name = "logic", "daq"
    hub.add(logic)
    hub.add(daq)
    try:
        project = Project.open(root)
        with open(os.path.join(root, "flows", "sync.flow.yaml")) as file:
            flow = project.complete(yaml_io.loads(file.read()))
        flow.nodes["sync"].params["duration"] = "2 s"
        for node in ("reference", "recording"):
            flow.nodes[node].params["duration"] = "2.5 s"
        engine = Engine(flow, mode="real", project=project, hub=hub)
        values: dict[str, list] = {}
        engine.subscribe(lambda event: event.kind == "value" and values.setdefault(
            f"{event.node}.{event.port}", []).append(event.value))
        result = engine.run(timeout=60)
        assert result.ok, result.error
        edges = {key: sum(int(np.count_nonzero(np.diff(block.values.astype(np.int8)))) for block in values[key])
                 for key in ("reference.GP17", "recording.P0.7")}
        # the sync signal of the logic analyzer on its own GP17 and, across processes, on P0.7 of the box
        assert edges["reference.GP17"] > 20 and abs(edges["recording.P0.7"] - edges["reference.GP17"]) <= 6
        assert values["align.uncertainty"][-1].value < 0.005
        # the simulators of the device list are given back as they were
        assert "wired" not in daq.simulation.circuit().get("P0.7", "")
        assert "wired" not in logic.simulation.circuit().get("GP17", "")
    finally:
        hub.close_all()
