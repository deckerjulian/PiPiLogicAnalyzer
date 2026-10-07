"""What a simulator simulates (scenarios, single channels), several simulators side by side and
simulated multi device sets."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from openscilab.core import c64_model, capture_io
from openscilab.driver.base import CaptureError
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import SimAddress, free_address, open_simulated, scenarios
from openscilab.driver.simulated.circuit import Replay, make_source
from openscilab.lab import Flow

ROOT = os.path.join(os.path.dirname(__file__), "..")
C64_DEMO = os.path.join(ROOT, "examples", "c64-demo.lac")


def capture(instrument, session, timeout=10.0):
    done = threading.Event()
    results = []
    error = instrument.capture.driver.start_capture(session, lambda args: (results.append(args), done.set()))
    assert error == CaptureError.NONE, error
    assert done.wait(timeout)
    assert results[0].success, results[0].error
    return results[0].session


def session_of(channels, rate=1_000_000, pre=0, post=2000, **options) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=pre, post_trigger_samples=post, **options)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    return session


# ----------------------------------------------------------------- addresses
def test_addresses_of_simulators():
    assert SimAddress.parse("sim:uno") == SimAddress("uno") and str(SimAddress("uno")) == "sim:uno"
    address = SimAddress.parse("pico*2#3")
    assert (address.profile, address.boards, address.instance) == ("pico", 2, 3)
    assert str(address) == "sim:pico*2#3" and address.spec == "pico*2"
    assert address.title("Simulation: Pico") == "Simulation: Pico × 2 (3)"
    with pytest.raises(ValueError):
        SimAddress.parse("pico*9")
    with pytest.raises(ValueError):
        SimAddress.parse("pico*two")
    assert str(free_address("uno", [])) == "sim:uno"
    assert str(free_address("uno", ["sim:uno", "sim:uno#2", "sim:pico"])) == "sim:uno#3"


def test_two_simulators_of_one_profile_do_not_share_anything():
    first, second = open_simulated("uno", fast=True), open_simulated("uno#2", fast=True)
    assert (first.uri, second.uri) == ("sim:uno", "sim:uno#2") and first.name != second.name
    scenarios.apply(second.simulated_driver, {"scenario": "uart", "channel": 5})
    assert "UART" not in " ".join(first.simulated_driver.circuit.describe().values())
    first.gpio.set_mode("D7", "output")
    first.gpio.write("D7", 1)
    assert second.gpio.mode("D7") == "input"
    assert first.simulated_driver.profile is not second.simulated_driver.profile


# ----------------------------------------------------------------- scenarios
def test_uart_on_a_channel_and_nothing_else():
    instrument = open_simulated("free", fast=True, clock=lambda: 0.0,
                                signals={"scenario": "uart", "text": "Hi\\n", "baud": 100_000, "channel": 3})
    driver = instrument.simulated_driver
    assert driver.signal_names == {2: "TX"}
    assert driver.circuit.describe()["D2"] == "UART 100000 Bd: 'Hi\\n'"
    result = capture(instrument, session_of([0, 2], post=4000, trigger_type=TriggerType.IMMEDIATE))
    idle, uart = (channel.samples for channel in result.capture_channels)
    assert not idle.any()  # the counter of the profile is gone
    assert 0 < uart.mean() < 1
    # the first byte after the idle gap: start bit, 'H' = 0x48 LSB first, stop bit (10 samples a bit)
    start = int(np.argmax(uart == 0))
    bits = [int(uart[start + 5 + 10 * index]) for index in range(10)]
    assert bits == [0, 0, 0, 0, 1, 0, 0, 1, 0, 1]


def test_a_single_channel_gets_its_own_signal_and_wiring_stays():
    instrument = open_simulated("uno", fast=True)
    driver = instrument.simulated_driver
    config = scenarios.apply(driver, {"scenario": "idle", "channels": {"D5": {"type": "square", "frequency": "1 kHz"}}})
    described = driver.circuit.describe()
    assert described["D5"].startswith("square 1 kHz") and described["D3"] == "wired to D7"  # the loop back stays
    assert described["D4"].startswith("low") and config["channels"] == {"D5": {"type": "square", "frequency": "1 kHz"}}
    assert scenarios.describe(config) == "Nothing connected + 1 channel set by hand"
    # an output of the device wins over the scenario
    instrument.gpio.set_mode("D5", "output")
    instrument.gpio.write("D5", 1)
    scenarios.apply(driver, {"scenario": "uart", "channel": 4})  # channel 4 is D5
    assert "UART" not in driver.circuit.describe()["D5"]
    scenarios.apply(driver, {"scenario": "default"})
    assert driver.circuit.describe()["D2"] == "button"


def test_what_does_not_fit_is_refused_and_changes_nothing():
    instrument = open_simulated("uno", fast=True)
    driver = instrument.simulated_driver
    before = dict(driver.circuit.sources)
    for config in ({"scenario": "c64"}, {"scenario": "uart", "channel": 99}, {"scenario": "nothing"},
                   {"scenario": "file", "path": "/nowhere.lac"}, {"channels": {"D99": {"type": "square"}}},
                   {"channels": {"D5": {"type": "square", "nonsense": 1}}}):
        with pytest.raises(scenarios.ScenarioError):
            scenarios.apply(driver, config)
    assert driver.circuit.sources == before and driver.signals["scenario"] == "default"
    reasons = {scenario.key: reason for scenario, reason in scenarios.available(driver)}
    assert reasons["uart"] == "" and "48 channels" in reasons["c64"]


def test_replay_repeats_a_recording():
    source = Replay(np.array([0, 1, 1, 0], dtype=np.uint8), 1000.0, "pattern")
    assert list(source.digital(0.0, 1000, 10)) == [0, 1, 1, 0, 0, 1, 1, 0, 0, 1]
    assert list(source.digital(0.0, 2000, 6)) == [0, 0, 1, 1, 1, 1]
    constant = Replay(np.ones(4, dtype=np.uint8), 1000.0)
    low, high = constant.envelope(0.0, 1000, 4000, 1000)
    assert low.min() == 1 and high.max() == 1  # a line that never changes is not "both levels"
    assert make_source({"type": "c64", "line": "A3"}).describe() == "C64 A3"
    with pytest.raises(ValueError):
        make_source({"type": "c64", "line": "A99"})


def test_a_capture_file_is_played_on_its_channels():
    instrument = open_simulated("pico", fast=True, clock=lambda: 0.0,
                                signals={"scenario": "file", "path": os.path.join(ROOT, "examples", "demo.lac")})
    original = capture_io.load_capture(os.path.join(ROOT, "examples", "demo.lac")).session
    numbers = [channel.channel_number for channel in original.capture_channels]
    result = capture(instrument, session_of(numbers, rate=original.frequency, post=1000,
                                            trigger_type=TriggerType.IMMEDIATE))
    # the device starts after its latency: that far into the recording
    first = round(instrument.simulated_driver.profile["latency"] * original.frequency)
    for played, recorded in zip(result.capture_channels, original.capture_channels):
        assert np.array_equal(played.samples, np.roll(recorded.samples, -first)[:1000])  # repeats at its end
    assert instrument.simulated_driver.signal_names[numbers[0]] == original.capture_channels[0].channel_name


# ------------------------------------------------------------- multi devices
def test_a_multi_device_of_simulated_boards():
    instrument = open_simulated("pico*2", fast=True)
    driver = instrument.capture.driver
    assert instrument.uri == "sim:pico*2" and instrument.name == "Simulation: Pico × 2"
    assert (driver.channel_count, driver.board_count, driver.channels_per_device) == (48, 2, 24)
    names = driver.channel_names()
    assert names[0] == "B1.GP2" and names[24] == "B2.GP2"
    assert instrument.gpio is None and instrument.monitor is None  # a set only captures
    assert "*" not in driver.driver_id
    # both boards carry the signals of the profile
    result = capture(instrument, session_of([0, 24], post=64, trigger_type=TriggerType.IMMEDIATE))
    assert np.array_equal(result.capture_channels[0].samples, result.capture_channels[1].samples)


def test_the_c64_bus_on_a_multi_device_is_the_demo_capture():
    """Two simulated Picos told to simulate the C64 bus, captured with the settings of the
    profile "C64 expansion port": the same samples as examples/c64-demo.lac."""
    demo = capture_io.load_capture(C64_DEMO).session
    instrument = open_simulated("pico*2", fast=True, signals={"scenario": "c64"})
    session = session_of([channel.channel_number for channel in demo.capture_channels], rate=demo.frequency,
                         pre=demo.pre_trigger_samples, post=demo.post_trigger_samples,
                         trigger_type=TriggerType.EDGE, trigger_channel=c64_model.PROFILE_CHANNELS["/RESET"])
    result = capture(instrument, session)
    assert result.pre_trigger_samples == demo.pre_trigger_samples
    for captured, expected in zip(result.capture_channels, demo.capture_channels):
        assert np.array_equal(captured.samples, expected.samples), expected.channel_name
    names = instrument.simulated_driver.signal_names
    assert names[0] == "Φ2" and names[24] == "A0" and names[14] == "A0 ref"


# --------------------------------------------------------------------- flows
def test_a_flow_tells_its_simulator_what_to_simulate(tmp_path):
    from openscilab.lab.engine import Engine

    flow = Flow("uart")
    flow.add_node("device.instrument", "sim", address="sim:free",
                  signals={"scenario": "uart", "text": "A", "baud": 100_000, "channel": 1})
    flow.add_node("device.capture", "cap", channels=["D0"], rate="1 MHz", samples=2000)
    flow.connect("sim.device", "cap.device")
    engine = Engine(flow, mode="virtual", data_dir=str(tmp_path))
    result = engine.run()
    assert result.ok, result.error
    driver = engine.devices["sim"].simulated_driver
    assert driver.signals["scenario"] == "uart" and "UART" in driver.circuit.describe()["D0"]


def test_two_simulators_of_one_profile_in_a_flow(tmp_path):
    from openscilab.lab.engine import Engine

    flow = Flow("two")
    flow.add_node("device.instrument", "a", address="sim:uno")
    flow.add_node("device.instrument", "b", address="sim:uno#2")
    for name in ("a", "b"):
        flow.add_node("gpio.write", f"write_{name}", pin="D7", level=1)
        flow.connect(f"{name}.device", f"write_{name}.device")
    engine = Engine(flow, mode="virtual", data_dir=str(tmp_path))
    assert engine.run().ok
    assert engine.devices["a"] is not engine.devices["b"]
    assert engine.devices["a"].uri == "sim:uno" and engine.devices["b"].uri == "sim:uno#2"


# ------------------------------------------------------------------------ UI
def sim_entry(shell, value):
    from openscilab.ui import devices

    backend = next(backend for backend in devices.backends() if backend.id == "sim")
    return next(entry for entry in backend.manual_entries() if entry.value == value)


def test_the_device_list_connects_a_simulator_again_and_again(shell):
    first = shell.connect_entry(sim_entry(shell, "uno"))
    second = shell.connect_entry(sim_entry(shell, "uno"))
    assert (first.uri, second.uri) == ("sim:uno", "sim:uno#2")
    assert first.name == "Simulation: Arduino Uno" and second.name == "Simulation: Arduino Uno (2)"
    assert shell.device_card(first) is not shell.device_card(second)
    shell.disconnect_instrument(first, ask=False)
    third = shell.connect_entry(sim_entry(shell, "uno"))
    assert third.uri == "sim:uno"  # the free address again
    # both are offered to flows as device nodes with their own address
    addresses = {preset.address for preset in shell.device_presets() if preset.kind == "open"}
    assert {"sim:uno", "sim:uno#2"} <= addresses


def test_a_simulated_multi_device_from_the_device_list(shell, monkeypatch):
    from openscilab.ui.dialogs.simulated_multi_dialog import SimulatedMultiDialog

    def choose(dialog):
        dialog.profile_box.setCurrentIndex(dialog.profile_box.findData("pico"))
        dialog.boards_box.setValue(2)
        assert "48 channels" in dialog.total_label.text() and "C64" in dialog.total_label.text()
        dialog._accept()
        return True

    monkeypatch.setattr(SimulatedMultiDialog, "exec", choose)
    instrument = shell.connect_entry(sim_entry(shell, None))
    assert instrument.uri == "sim:pico*2" and instrument.capture.driver.board_count == 2
    card = shell.device_card(instrument)
    view = shell.show_data(instrument)
    assert card.signals_panel is not None and view.capture_controls.channel_table.rowCount() == 48


def test_the_signals_tab_tells_the_device_what_to_simulate(shell):
    from PySide6.QtWidgets import QLineEdit, QSpinBox

    from openscilab.driver.simulated.stored import stored_signals
    from openscilab.ui.devices.simulated import open_at

    instrument = shell.connect_entry(sim_entry(shell, "free"))
    card = shell.device_card(instrument)
    panel = card.signals_panel
    assert "Signals" in [card.tabs.tabText(index) for index in range(card.tabs.count())]
    # a scenario that needs more channels is shown, but cannot be chosen
    index = panel.scenario_box.findData("c64")
    assert not panel.scenario_box.model().item(index).isEnabled()
    panel.scenario_box.setCurrentIndex(panel.scenario_box.findData("uart"))
    panel.findChild(QLineEdit, "param-text").setText("Hi")
    panel.findChild(QLineEdit, "param-baud").setText("9600")
    panel.findChild(QSpinBox, "param-channel").setValue(4)
    assert panel.apply()
    driver = instrument.simulated_driver
    assert driver.circuit.describe()["D3"] == "UART 9600 Bd: 'Hi'"
    assert panel.table.item(3, 2).text().startswith("UART") and panel.table.item(0, 2).text().startswith("low")
    assert card.controller.channel_names().get(3) == "TX"  # named for the next capture
    assert stored_signals("sim:free")["scenario"] == "uart"
    # wrong settings are said in the tab and change nothing
    panel.findChild(QLineEdit, "param-baud").setText("fast")
    assert not panel.apply() and not panel.message.isHidden()
    assert driver.circuit.describe()["D3"] == "UART 9600 Bd: 'Hi'"
    # the next time the simulator is opened it simulates the same
    shell.disconnect_instrument(instrument, ask=False)
    again = open_at("sim:free", shell.hub)
    assert again.simulated_driver.signals["scenario"] == "uart"


def test_a_channel_gets_a_signal_by_hand(shell, monkeypatch):
    from openscilab.ui.devices.signals_panel import SourceDialog

    instrument = shell.connect_entry(sim_entry(shell, "uno"))
    panel = shell.device_card(instrument).signals_panel

    def choose(dialog):
        dialog.kind_box.setCurrentIndex(dialog.kind_box.findData("square"))
        dialog.settings_edit.setText("frequency: 2 kHz")
        dialog._accept()
        return dialog.result()

    monkeypatch.setattr(SourceDialog, "exec", choose)
    row = panel.nets().index("D5")
    assert not panel.edit_button.isEnabled()  # (for the channel selected in the table)
    panel.table.selectRow(row)
    assert panel.edit_button.isEnabled()
    panel.edit_button.click()
    assert instrument.simulated_driver.circuit.describe()["D5"].startswith("square 2 kHz")
    assert "set by hand" in panel.table.item(row, 2).text()

    def wrong(dialog):
        dialog.kind_box.setCurrentIndex(dialog.kind_box.findData("square"))
        dialog.settings_edit.setText("frequency: [")
        dialog._accept()
        assert not dialog.message.isHidden()
        return 0

    monkeypatch.setattr(SourceDialog, "exec", wrong)
    assert not panel.edit_channel(row)

    def back(dialog):
        dialog.kind_box.setCurrentIndex(0)  # as the scenario says
        dialog._accept()
        return dialog.result()

    monkeypatch.setattr(SourceDialog, "exec", back)
    assert panel.edit_channel(row)
    assert "D5" not in instrument.simulated_driver.signals["channels"]


def test_the_c64_bus_from_the_device_card_to_the_data_view(shell):
    """The whole way: a multi device of two simulated Picos, told to simulate the C64 bus,
    captured from its card."""
    import time

    from PySide6.QtWidgets import QApplication

    from openscilab.ui.devices.simulated import open_at

    instrument = open_at("sim:pico*2", shell.hub)
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    panel = card.signals_panel
    panel.scenario_box.setCurrentIndex(panel.scenario_box.findData("c64"))
    assert panel.apply()
    names = card.controller.channel_names()
    assert names[0] == "Φ2" and names[24] == "A0"
    session = session_of(range(48), rate=20_000_000, pre=1000, post=30000, trigger_type=TriggerType.EDGE,
                         trigger_channel=c64_model.PROFILE_CHANNELS["/RESET"])
    assert card.controller.capture(session)
    deadline = time.monotonic() + 15
    view = card.controller.view
    while time.monotonic() < deadline and (card.controller.is_capturing or view.model.session is None
                                           or view.model.sample_count < 31000):
        QApplication.processEvents()
        time.sleep(0.01)
    captured = view.model.session
    assert captured.pre_trigger_samples == 1000 and view.model.sample_count == 31000
    reset = next(channel for channel in captured.capture_channels if channel.channel_number == 2).samples
    assert not reset[:1000].any() and reset[1000:].all()  # triggered on the end of the reset


def test_the_session_brings_back_simulators_with_their_signals(shell):
    from openscilab.ui.shell import session as shell_session

    instrument = shell.connect_entry(sim_entry(shell, "uno"))
    second = shell.connect_entry(sim_entry(shell, "uno"))
    panel = shell.device_card(second).signals_panel
    panel.scenario_box.setCurrentIndex(panel.scenario_box.findData("i2c"))
    assert panel.apply()
    state = shell_session.session_state(shell)
    assert state["devices"] == ["sim:uno", "sim:uno#2"]
    for item in (instrument, second):
        shell.disconnect_instrument(item, ask=False)
    assert shell_session.reconnect(shell, state["devices"]) == []
    restored = {item.uri: item for item in shell.hub.instruments()}
    assert restored["sim:uno#2"].simulated_driver.signals["scenario"] == "i2c"
    assert restored["sim:uno"].simulated_driver.signals["scenario"] == "default"
