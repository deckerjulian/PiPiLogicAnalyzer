"""The Timing tab of the device card: measuring the latency with a loopback without a flow (wired in
the simulator), the sample clock of a device (timing.align follows it), a sync output, and the time of
captures of the data view; the wires and the USB link of a simulator, kept for it."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from openscilab.core import timing, timing_tools
from openscilab.driver.simulated import open_simulated, set_usb, set_wiring, usb_of, wiring_of


def settle(times: int = 20) -> None:
    for _ in range(times):
        QApplication.processEvents()


def wait(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        settle(5)
        time.sleep(0.01)
    return condition()


# ------------------------------------------------------------------ core
def test_the_latency_is_measured_without_a_flow():
    pico = open_simulated("pico", wiring=[{"from": "GP16", "to": "GP17"}])
    latency = timing_tools.measure_latency(pico, "GP16", "GP17", rate=100_000)
    usb = pico.simulated_driver.profile["usb"]
    # half the loop is taken as the latency of the input (output and input alike); the simulator's
    # output is immediate, its input over USB late: at least half its latency (more when the computer
    # is busy: the shortest of ten loops counts)
    assert usb["latency"] / 2 <= latency.value < 0.05
    assert latency.source == "loopback"
    assert timing.stored_latency(timing.calibration_key(pico, "stream", 100_000)) is not None
    # a capture as well (the simulator records what the outputs do while it runs, as the device)
    captured = timing_tools.measure_latency(pico, "GP16", "GP17", rate=100_000, mode="capture", samples=50_000,
                                            store=False)
    assert captured.value > 0
    unwired = open_simulated("pico")
    with pytest.raises(timing_tools.LatencyError, match="is GP16 wired to it"):
        timing_tools.measure_latency(unwired, "GP16", "GP17", store=False)
    with pytest.raises(timing_tools.LatencyError, match="needs an output"):
        timing_tools.measure_latency(open_simulated("dho924s"), "CH1", "CH2", store=False)


def test_a_simulator_is_wired_and_its_usb_link_set():
    pico = open_simulated("pico")
    driver = pico.simulated_driver
    set_wiring(driver, [{"from": "GP16", "to": "GP17"}])
    assert wiring_of(driver) == [{"from": "GP16", "to": "GP17"}]
    gpio = pico.gpio
    gpio.write("GP16", 1)
    assert gpio.read("GP17") == 1  # the wire
    with pytest.raises(ValueError, match="no pin"):
        set_wiring(driver, [{"from": "GP16", "to": "XX"}])
    with pytest.raises(ValueError, match="loop"):
        set_wiring(driver, [{"from": "GP16", "to": "GP17"}, {"from": "GP17", "to": "GP16"}])
    assert wiring_of(driver) == [{"from": "GP16", "to": "GP17"}]  # (unchanged)
    assert usb_of(driver)["latency"] == pytest.approx(0.0012) and not driver.knows_time
    set_usb(driver, {"latency": 0.005, "jitter": 0.0})
    assert usb_of(driver)["latency"] == 0.005 and usb_of(driver)["frame"] == 0.001
    set_usb(driver, None)
    assert usb_of(driver) is None and driver.knows_time
    with pytest.raises(ValueError):
        set_usb(driver, {"latency": 5})


def test_the_sample_clock_of_a_device_decides_what_align_fits():
    pico = open_simulated("pico")
    assert timing_tools.device_config(pico)["clock"] == timing_tools.CLOCK_OWN and not timing_tools.shares_clock(pico)
    timing_tools.set_device_config(pico, clock=timing_tools.CLOCK_SHARED)
    assert timing_tools.shares_clock(pico) and timing_tools.shares_clock(open_simulated("pico"))  # (by address)
    assert not timing_tools.shares_clock(open_simulated("uno"))
    from openscilab.lab.nodes import timing as timing_nodes  # noqa: F401 - registers the nodes
    from openscilab.lab.nodes.registry import default_registry

    params = {param.name: param for param in default_registry.get("timing.align").params}
    assert params["drift"].default == "auto" and "auto" in params["drift"].choices


def test_a_sync_output_drives_its_pin_until_stopped():
    pico = open_simulated("pico")
    output = timing_tools.start_sync_output(pico, "GP16", seed=3)
    try:
        assert timing_tools.sync_output_of(pico) is output
        assert wait(lambda: len(output.edges) >= 3, 2.0)
        from openscilab_device.timing import sync_intervals

        intervals = sync_intervals(3)
        expected = [next(intervals) for _ in range(3)]
        times = [at for at, _level in output.edges[:3]]
        assert [level for _at, level in output.edges[:3]] == [1, 0, 1]
        assert times[2] - times[1] == pytest.approx(expected[2], abs=0.01)
    finally:
        timing_tools.stop_sync_output(pico)
    assert timing_tools.sync_output_of(pico) is None and not output.running and pico.gpio.read("GP16") == 0


# -------------------------------------------------------------------- card
def test_the_timing_tab_measures_wires_and_runs_a_sync_output(shell, monkeypatch):
    from openscilab.ui.devices.simulated import open_at, stored_circuit

    pico = open_at("sim:pico", shell.hub)
    shell.hub.add(pico)
    card = shell.open_device_card(pico)
    card.tabs.setCurrentWidget(card.timing_page)
    settle()
    assert not card.latency_box.isHidden() and not card.wire_button.isHidden()
    assert "emulates a USB link" in card.latency_note.text()
    card.loop_output.setCurrentText("GP16")
    card.loop_input.setCurrentText("GP17")
    assert card.wire_loopback()
    assert wiring_of(pico.simulated_driver) == [{"from": "GP16", "to": "GP17"}]
    assert stored_circuit("sim:pico")["wiring"] == [{"from": "GP16", "to": "GP17"}]  # kept for the simulator
    assert card.signals_panel.wire_table.rowCount() == 1
    assert card.measure_latency()
    assert "kept for its captures" in card.banner.label.text()
    assert "stream at 100 kHz" in card.latency_label.text()
    # the sample clock is kept for the device
    card.clock_box.setCurrentIndex(card.clock_box.findData(timing_tools.CLOCK_SHARED))
    card.clock_box.activated.emit(card.clock_box.currentIndex())
    assert timing_tools.shares_clock(pico) and "Shared" in card.timing_label.text()
    # a sync output, stopped with the card
    card.sync_pin.setCurrentText("GP18")
    assert card.toggle_sync_output() and card.sync_button.text() == "Stop"
    assert timing_tools.device_config(pico)["sync_pin"] == "GP18"
    assert wait(lambda: len(timing_tools.sync_output_of(pico).edges) >= 1, 2.0)
    card._update_timing()
    assert "running on GP18" in card.sync_state.text()
    shell.area.close_document(card, force=True)
    settle()
    assert timing_tools.sync_output_of(pico) is None
    # the next time the simulator is connected it has its wire again
    again = open_at("sim:pico")
    assert wiring_of(again.simulated_driver) == [{"from": "GP16", "to": "GP17"}]


def test_the_usb_link_of_a_simulator_is_set_in_its_signals_tab(shell):
    from openscilab.ui.devices.simulated import open_at, stored_circuit

    uno = open_at("sim:uno", shell.hub)
    shell.hub.add(uno)
    card = shell.open_device_card(uno)
    panel = card.signals_panel
    assert not panel.usb_box.isChecked()  # (the Uno simulator knows its time)
    panel.usb_box.setChecked(True)
    panel.usb_spins["latency"].setValue(2.5)
    assert panel.apply_usb()
    assert usb_of(uno.simulated_driver)["latency"] == pytest.approx(0.0025)
    assert stored_circuit("sim:uno")["usb"]["latency"] == pytest.approx(0.0025)
    assert usb_of(open_at("sim:uno").simulated_driver)["latency"] == pytest.approx(0.0025)
    panel.usb_box.setChecked(False)
    assert panel.apply_usb() and usb_of(uno.simulated_driver) is None
    assert stored_circuit("sim:uno")["usb"] is False and usb_of(open_at("sim:uno").simulated_driver) is None
    assert panel.add_wire("D9", "D3") and panel.remove_wire(0) and wiring_of(uno.simulated_driver) == []


def test_a_capture_of_the_data_view_has_a_time(shell):
    pico = open_simulated("pico")
    shell.hub.add(pico)
    card = shell.open_device_card(pico)
    view = shell.show_data(pico)
    controller = card.controller
    timing.store_latency(timing.calibration_key(pico, "stream", 1e5), timing.Latency(1e-3, 1e-4, "loopback"))
    assert view.capture()
    assert wait(lambda: not controller._running)
    state = timing.timing_of(pico).state()
    assert state is not None and state.method == timing.METHOD_BOUNDS  # (USB emulated: between start and arrival)
    card._update_timing()
    assert "between start command and arrival" in card.timing_label.text()
    # a stream at a rate with a measured latency is placed with it
    session = controller.settings().clone_settings()
    session.acquisition_mode = "stream"
    session.frequency = 100_000  # (all channels over the 800 kB/s of the Pico's USB)
    session.post_trigger_samples, session.pre_trigger_samples = 20_000, 0
    from openscilab.driver.models import TriggerType

    session.trigger_type = TriggerType.IMMEDIATE
    assert controller.capture(session)
    assert wait(lambda: not controller._running)
    state = timing.timing_of(pico).state()
    assert state.method == timing.METHOD_LATENCY and state.latency.value == pytest.approx(1e-3)
