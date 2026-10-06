"""The device card: pins, switching outputs, pulses, the monitor, all outputs safe, markers in
running captures and recording the monitor."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QPushButton, QWidget

from openscilab.core.instrument import MODE_INPUT, MODE_OUTPUT
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated
from openscilab.ui import messages
from openscilab.ui.documents.device import COL_ACTION, COL_CAN, COL_CHANNEL, COL_MODE, COL_PIN


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


@pytest.fixture
def asked(monkeypatch):
    questions = []
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: questions.append(args[2]) or True)
    return questions


@pytest.fixture
def card(shell):
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    return shell.open_device_card(instrument)


def row_of(card, pin: str) -> int:
    for row in range(card.pin_table.rowCount()):
        if card.pin_table.item(row, COL_PIN).text() == pin:
            return row
    raise KeyError(pin)


def test_the_card_shows_the_pins(card):
    tabs = [card.tabs.tabText(index) for index in range(card.tabs.count())]
    assert tabs == ["Pins", "Signals", "Events", "Send", "Timing", "Details"]
    assert not card.strip.isHidden() and not card.pin_table.isColumnHidden(COL_MODE)
    assert [pin.name for pin in card.strip.pins][:3] == ["D0", "D1", "D2"]
    mode = card.pin_table.cellWidget(row_of(card, "D0"), COL_MODE)
    assert isinstance(mode, QComboBox) and not mode.isEnabled()  # reserved: greyed out
    assert "reserved" in card.pin_table.item(row_of(card, "D0"), COL_CAN).text()
    assert card.pin_table.item(row_of(card, "A0"), COL_CHANNEL).text() == "A0"  # analog input 0
    assert card.pin_table.item(row_of(card, "D2"), COL_CHANNEL).text() == "0"  # logic channel 0
    modes = card.pin_table.cellWidget(row_of(card, "D9"), COL_MODE)
    assert [modes.itemData(index) for index in range(modes.count())] == ["input", "input_pullup", "output", "pwm"]
    assert not card.grab().isNull()


def test_switching_d7_shows_the_edge_at_d3_in_the_monitor(card, asked):
    switch = card.pin_table.cellWidget(row_of(card, "D7"), COL_ACTION).findChild(QPushButton, "switch-D7")
    switch.setChecked(True)
    assert asked == ["D7 will drive 5 V."]  # the level warning, once
    assert card.instrument.gpio.mode("D7") == MODE_OUTPUT
    card.monitor_rate.setValue(100)
    card.monitor_box.setChecked(True)
    try:
        assert wait_for(lambda: card.value_labels["D3"].text() == "● 1")
        assert card.strip.levels.get("D3") == 1
        switch.setChecked(False)
        assert wait_for(lambda: card.value_labels["D3"].text() == "○ 0")
        assert len(asked) == 1
    finally:
        card.monitor_box.setChecked(False)
    assert wait_for(lambda: card.event_list.count() >= 2)


def test_a_pulse_and_all_outputs_safe(card, asked):
    actions = []
    card.action_performed.connect(lambda instrument, text: actions.append(text))
    assert card.pulse("D8", 0.002)
    assert actions == ["D8: pulse 2 ms"]
    assert card.write("D7", 1)
    card.setFocus()
    card.all_safe()
    gpio = card.instrument.gpio
    assert gpio.mode("D7") == MODE_INPUT and gpio.mode("D8") == MODE_INPUT
    assert actions[-1] == "all outputs safe"


def test_refusing_the_level_warning_drives_nothing(card, monkeypatch):
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: False)
    assert not card.write("D7", 1)
    assert card.instrument.gpio.mode("D7") == MODE_INPUT


def test_a_reserved_pin_is_refused(card, asked, monkeypatch):
    warnings = []
    monkeypatch.setattr(messages, "warning", lambda parent, title, text, *rest: warnings.append(text))
    assert not card.write("D0", 1)
    assert "reserved" in warnings[0]


def test_esc_is_all_outputs_safe(card, asked, qtbot):
    card.write("D7", 1)
    card.tabs.setCurrentIndex(0)  # Esc means "all outputs safe" on the Pins tab
    card.activateWindow()
    card.pin_table.setFocus()
    qtbot.keyClick(card.pin_table, Qt.Key_Escape)
    assert card.instrument.gpio.mode("D7") == MODE_INPUT


def test_actions_are_markers_in_a_running_capture(shell, card, make_dataview, asked):
    instrument = card.instrument
    window = make_dataview()
    window.use_instrument(instrument)
    # two seconds: long enough to be seen running also on a slow machine
    session = CaptureSession(frequency=10_000, pre_trigger_samples=0, post_trigger_samples=20_000,
                             trigger_type=TriggerType.IMMEDIATE, acquisition_mode="stream")
    session.capture_channels = [AnalyzerChannel(channel_number=1, channel_name="D3")]
    window._begin_capture(session)
    assert wait_for(lambda: window.model.is_live)
    assert card.write("D7", 1)
    assert [bookmark.name for bookmark in window.model.bookmarks] == ["Simulation: Arduino Uno: D7 = 1"]
    assert wait_for(lambda: not window.driver.is_capturing, 20)
    for _ in range(20):
        QApplication.processEvents()
    names = [bookmark.name for bookmark in window.model.bookmarks]
    assert names == ["Simulation: Arduino Uno: D7 = 1"]  # kept in the completed capture
    marker = window.model.bookmarks[0].sample
    samples = window.model.session.capture_channels[0].samples
    edge = int(np.argmax(samples))
    # the marker is as exact as the live display: at most half a second before the edge (on a busy
    # machine the display lags behind the stream; at rest it is within a block or two)
    assert samples[0] == 0 and marker <= edge <= marker + 5000


def test_recording_the_monitor_into_an_analyzer_document(shell, card, asked):
    from openscilab.ui.documents.dataview import DataView

    card.monitor_rate.setValue(200)
    document = shell.record_monitor(card.instrument)
    assert isinstance(document, DataView) and document.recording is not None
    assert wait_for(lambda: document.recording.count > 10)
    assert card.write("D7", 1)
    assert wait_for(lambda: document.recording.count > 40)
    assert wait_for(lambda: document.model.session is not None)
    session = document.stop_recording()
    assert document.recording is None and session.frequency == 200
    names = [channel.channel_name for channel in session.capture_channels]
    assert "D3" in names and [channel.channel_name for channel in session.analog_channels][:1] == ["A0"]
    d3 = session.capture_channels[names.index("D3")].samples
    assert d3[0] == 0 and d3[-1] == 1
    assert [bookmark.name for bookmark in document.model.bookmarks] == ["Simulation: Arduino Uno: D7 = 1"]
    assert document.dirty


def test_stimulus_and_capture_on_another_instrument(shell, card, asked):
    """Arm a capture on a second simulator (over the hub), then pulse; the capture sees the pulse
    on the wired input of the uno and the marker."""
    instrument = card.instrument
    window = shell.show_data(instrument)
    session = CaptureSession(frequency=10_000, pre_trigger_samples=0, post_trigger_samples=400,
                             trigger_type=TriggerType.EDGE, trigger_channel=1)
    session.capture_channels = [AnalyzerChannel(channel_number=1, channel_name="D3")]
    window.model.set_session(session)  # the last capture's settings
    card._fill_stimulus_targets()
    assert card.stimulus_target.findText(instrument.name) >= 0
    card.pin_table.selectRow(row_of(card, "D7"))
    widths = card.pin_table.cellWidget(row_of(card, "D7"), COL_ACTION).findChild(QDoubleSpinBox, "width-D7")
    widths.setValue(20)
    card.stimulus_target.setCurrentIndex(card.stimulus_target.findText(instrument.name))
    card.stimulus_button.click()
    assert wait_for(lambda: window._running_capture is None and window.model.session is not session, 20)
    for _ in range(20):
        QApplication.processEvents()
    captured = window.model.session.capture_channels[0].samples
    assert captured[0] == 1 and int(captured[:400].sum()) == pytest.approx(200, abs=2)  # 20 ms at 10 kSa/s
    assert any("D7: pulse" in bookmark.name for bookmark in window.model.bookmarks)


def test_capturing_happens_in_the_data_view(shell):
    """The card's *Capture…* opens the data view of the device; settings, start and the captures are there."""
    from openscilab.ui.documents.dataview import DataView

    instrument = open_simulated("free")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    assert not hasattr(card, "capture_page") and card.data_button.text() == "Capture..."
    assert card.start_capture()
    view = card.controller.view
    assert isinstance(view, DataView) and view.source is card.controller and view.instrument is instrument
    assert shell.active_document() is view  # capturing goes on there
    controls = view.capture_controls
    assert controls.controller is card.controller and view.device_combo.currentText() == instrument.name
    assert controls.capture_button.isEnabled() and not view.repeat_button.isEnabled()
    assert view.capture()  # the quick settings of the capture tool bar
    assert wait_for(lambda: view.model.session is not None and not instrument.capture.driver.is_capturing)
    assert view.model.sample_count > 0
    assert wait_for(lambda: view.repeat_button.isEnabled())
    first = view.model.session
    assert view.repeat_capture()
    assert wait_for(lambda: view.model.session is not first and not instrument.capture.driver.is_capturing)
    # the device leaves the hub: the view keeps the data, without a source
    shell.hub.remove(instrument.name)
    assert view.source is None and controls.controller is None and not controls.capture_button.isEnabled()
    assert view.device_combo.currentData() is None


def test_the_data_view_chooses_the_device(shell):
    first, second = open_simulated("free"), open_simulated("uno")
    shell.hub.add(first)
    shell.hub.add(second)
    view = shell.new_data_view()
    names = [view.device_combo.itemText(i) for i in range(view.device_combo.count())]
    assert names == ["No device", first.name, second.name, "Connect a device..."]
    view.device_combo.setCurrentIndex(2)
    view.device_combo.activated.emit(2)
    assert view.instrument is second and view.capture_controls.controller.instrument is second
    view.device_combo.setCurrentIndex(0)
    view.device_combo.activated.emit(0)
    assert view.source is None
    requested = []
    view.devices_requested.connect(lambda: requested.append(True))
    assert not view.capture() and requested  # without a device: the device list


def test_applied_settings_name_the_channels(shell, monkeypatch):
    """*Apply* in the settings keeps them for the device: the card shows the channel names."""
    from openscilab.core import capture_io, settings
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog, capture_settings_file

    instrument = open_simulated("free")
    shell.hub.add(instrument)
    driver = instrument.capture.driver
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=1000,
                             trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="CLK"),
                                AnalyzerChannel(channel_number=9, channel_name="UART")]
    settings.persist_settings(capture_settings_file(driver), capture_io.session_to_dict(session, include_samples=False))
    card = shell.open_device_card(instrument)
    view = shell.show_data(instrument)
    controls = view.capture_controls
    assert not controls.repeat_button.isEnabled()  # nothing captured yet to capture again
    assert "1 MHz" in controls.summary_label.text()  # the stored settings are those of the next capture

    def apply(dialog):
        assert dialog.apply_button is not None
        dialog.apply_button.click()
        return dialog.result()

    monkeypatch.setattr(CaptureDialog, "exec", apply)
    assert controls.capture_settings() is False  # applied, not captured
    assert not driver.is_capturing and card.controller.channel_names() == {0: "CLK", 9: "UART"}
    assert card.pin_table.item(row_of(card, "CH1"), 2).text() == "CLK"
    assert card.pin_table.item(row_of(card, "CH10"), 2).text() == "UART"
    names = {controls.channel_table.item(row, 0).text(): controls.channel_table.item(row, 2).text()
             for row in range(controls.channel_table.rowCount())}
    assert names["0"] == "CLK" and names["1"] == "–"
    assert "2 channels" in controls.summary_label.text() and "1 MHz" in controls.summary_label.text()


def test_a_declined_switch_goes_back_to_off(card, monkeypatch):
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: False)
    switch = card.pin_table.cellWidget(row_of(card, "D7"), COL_ACTION).findChild(QPushButton, "switch-D7")
    switch.click()
    assert not switch.isChecked() and switch.text() == "Off"


def test_pwm_pins_show_all_their_actions(card):
    card.tabs.setCurrentIndex(0)
    QApplication.processEvents()
    actions = card.pin_table.cellWidget(row_of(card, "D3"), COL_ACTION)
    names = {child.objectName() for child in actions.findChildren(QWidget)}
    assert {"switch-D3", "width-D3", "frequency-D3", "duty-D3"} <= names
    for child in actions.findChildren(QPushButton):
        assert child.width() >= child.minimumSizeHint().width()  # nothing is cut off
