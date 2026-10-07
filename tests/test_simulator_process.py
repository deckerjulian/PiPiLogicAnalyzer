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
