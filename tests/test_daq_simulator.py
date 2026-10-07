"""The local DAQ simulator (``sim:daq``): analog inputs and outputs, digital lines, a USB link, a
loopback and a sample clock that drifts - its time has to be found as with the real box."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from openscilab.core import timing_tools
from openscilab.core.instrument import AnalogInFacet, AnalogOutFacet
from openscilab.driver.simulated import open_simulated
from openscilab.driver.simulated.profiles import available_profiles


def test_the_daq_simulator_is_a_usb_daq():
    assert "daq" in available_profiles()
    daq = open_simulated("daq")
    driver = daq.simulated_driver
    assert daq.name == "Simulation: USB DAQ" and driver.analog_channel_names() == [f"AI{n}" for n in range(8)]
    assert {"GPIO", "DAC", "ANALOG=8", "CONTINUOUS_STREAM"} <= driver.capabilities()
    assert not driver.knows_time and driver.drift == pytest.approx(30e-6)  # USB, a drifting clock
    # the loopbacks of the profile
    daq.gpio.write("P0.0", 1)
    assert daq.gpio.read("P0.1") == 1
    daq.facet(AnalogOutFacet).set_voltage("AO0", 3.0)
    assert daq.facet(AnalogInFacet).read(["AI2"])["AI2"] == pytest.approx(3.0, abs=0.02)
    # its latency over the emulated USB, measured with the digital loopback
    latency = timing_tools.measure_latency(daq, "P0.0", "P0.1", rate=10_000, store=False)
    assert 0.001 <= latency.value < 0.05
    # faster than it streams: said so before anything is switched
    assert timing_tools.loopback_rate_limit(daq, "P0.1") == 50_000
    with pytest.raises(timing_tools.LatencyError, match="streams at most 50 kHz"):
        timing_tools.measure_latency(daq, "P0.0", "P0.1", rate=100_000, store=False)


def test_a_drifting_sample_clock_takes_its_samples_on_the_true_clock():
    daq = open_simulated("daq")
    driver = daq.simulated_driver
    driver.set_drift(0.0)
    start = driver.clock() + 1.0
    exact = driver.sample([2], start, 10_000, 10_000)[2]  # P0.2: a 100 Hz square, one second
    driver.set_drift(10_000.0)  # 1 % fast: a second of its clock is 0.99 s of the true one
    fast = driver.sample([2], start, 10_000, 10_000)[2]

    def edges(values) -> int:
        return int(np.count_nonzero(np.diff(values.astype(np.int8)) == 1))

    assert edges(exact) == pytest.approx(100, abs=1)
    assert edges(fast) == pytest.approx(99, abs=1)  # fewer periods in what it calls a second
    assert driver.true_time(driver._drift_origin + 1.0) == pytest.approx(driver._drift_origin + 1.0 / 1.01)


def test_the_signals_tab_sets_the_drift_and_keeps_it(shell):
    from openscilab.ui.devices.simulated import open_at, stored_circuit

    daq = open_at("sim:daq", shell.hub)
    shell.hub.add(daq)
    card = shell.open_device_card(daq)
    panel = card.signals_panel
    assert panel.usb_box.isChecked() and panel.drift_spin.value() == pytest.approx(30.0)
    panel.drift_spin.setValue(150.0)
    assert panel.apply_usb()
    assert daq.simulated_driver.drift == pytest.approx(150e-6) and stored_circuit("sim:daq")["drift"] == 150.0
    assert open_at("sim:daq").simulated_driver.drift == pytest.approx(150e-6)
    time.sleep(0.01)


def test_the_timing_tab_measures_with_the_loopback_of_the_profile(shell):
    from openscilab.driver.simulated import wiring_of
    from openscilab.ui.devices.simulated import open_at

    daq = open_at("sim:daq", shell.hub)
    shell.hub.add(daq)
    card = shell.open_device_card(daq)
    panel = card.signals_panel
    # the wires of the profile are listed (not removable), the added ones first
    assert panel.wire_table.rowCount() == 2 and panel.wire_table.item(0, 0).text() == "P0.0"
    assert "of the profile" in panel.wire_table.item(0, 1).text() and not panel.remove_wire_button.isEnabled()
    # the Timing tab starts with the wire of the profile, at a rate the DAQ streams
    assert (card.loop_output.currentText(), card.loop_input.currentText()) == ("P0.0", "P0.1")
    rates = [card.loop_rate.itemText(index) for index in range(card.loop_rate.count())]
    assert rates == ["10 kHz", "50 kHz"] and card.loop_rate.currentText() == "50 kHz"
    assert card.wire_loopback() and wiring_of(daq.simulated_driver) == []  # (already wired)
    assert "already wired" in card.banner.label.text()
    # the reverse wire would be a loop: said on the Timing tab
    card.loop_output.setCurrentText("P0.1")
    card.loop_input.setCurrentText("P0.0")
    assert not card.wire_loopback() and "loop" in card.banner.label.text()
    card.loop_output.setCurrentText("P0.3")
    card.loop_input.setCurrentText("P0.4")
    assert card.wire_loopback() and wiring_of(daq.simulated_driver) == [{"from": "P0.3", "to": "P0.4"}]
    assert panel.wire_table.rowCount() == 3 and panel.remove_wire_button.isEnabled()
    card.loop_rate.setCurrentText("10 kHz")
    assert card.measure_latency(), card.banner.label.text()
    assert panel.remove_wire(0) and wiring_of(daq.simulated_driver) == []


# ------------------------------------------------------- compact controls
def test_smaller_text_makes_the_controls_tighter():
    from openscilab.core import preferences
    from openscilab.ui import theme

    assert preferences.DEFAULTS["appearance.font_size"] == 10 and theme.font_size() == 10
    assert theme.density(12) == 1.0 and theme.density(10) == pytest.approx(10 / 12) and theme.density(16) == 1.0
    large, small = theme.build_stylesheet(12), theme.build_stylesheet(10)
    assert "padding: 4px 10px;" in large and "padding: 3px 8px;" in small  # (the menu bar)
    assert "font-size: 10px;" in small and "icon-size: 14px;" in small and "icon-size: 16px;" in large
    assert "border-bottom: 2px solid" in small  # (lines stay as they are)
    assert theme.icon_px(16, 10) == 14 and theme.icon_px(16, 12) == 16 and theme.scaled(1, 10) == 1
    assert theme.build_stylesheet(14) == theme._stylesheet(14)  # (larger text: the spacing as drawn)
    from openscilab.ui.view_model import DEFAULT_CHANNEL_HEIGHT

    assert DEFAULT_CHANNEL_HEIGHT == 40


# ---------------------------------------------------- analog streams in flows
@pytest.mark.parametrize("channels", [["AI0", "AI1"], ["P0.2", "AI0"]])
def test_a_flow_streams_analog_inputs(channels):
    from openscilab.core import signals
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    flow = yaml_io.loads(f"""
flow: Stream
nodes:
  daq: {{type: device.instrument, address: "sim:daq"}}
  stream: {{type: device.stream, channels: [{', '.join(channels)}], rate: 1 kHz, duration: 0.3 s}}
edges:
  - daq.device -> stream.device
""")
    engine = Engine(flow, mode="real")
    values: dict[str, list] = {}
    engine.subscribe(lambda event: event.kind == "value" and values.setdefault(event.port, []).append(event.value))
    assert engine.run(timeout=30).ok
    blocks = values["AI0"]
    assert all(isinstance(block, signals.Analog) and block.unit == "V" for block in blocks)
    samples = np.concatenate([block.values for block in blocks])
    assert len(samples) == 300 and 1.5 < samples.max() <= 2.05  # the 50 Hz sine of 2 V on AI0
    assert "AI0" in values["capture"][0].analog
