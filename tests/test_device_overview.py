"""The device card with everything about a device in one place (no extra *Device information*), its
profiles, restart and the time of its samples; the simulators in one group of the device list;
disconnecting every device at once; views of a flow's data that do not capture; the connected
hardware without simulators and with the way firmware gets onto a board; tabs that look alike."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from openscilab.core import preferences, timing
from openscilab.driver.base import CAPABILITY_RESTART
from openscilab.driver.simulated import open_simulated
from openscilab.ui import messages

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def settle() -> None:
    for _ in range(20):
        QApplication.processEvents()


def card_of(shell, profile: str = "pico"):
    instrument = open_simulated(profile)
    shell.hub.add(instrument)
    return shell.open_device_card(instrument)


# ------------------------------------------------------------------ device card
def test_the_details_tab_holds_what_the_device_information_showed(shell):
    card = card_of(shell)
    texts = [action.text() for action in card.device_actions]
    assert "Device information..." not in texts and not hasattr(card, "action_info")
    assert texts[0] == "Capture settings..." and "Restart device..." in texts and "Copy the details" in texts
    # the capture limits of the device, by the number of channels
    assert not card.limits_table.isHidden() and card.limits_table.rowCount() == 3
    assert card.limits_table.item(0, 0).text() == "8 channels" and card.limits_table.item(0, 4).text() == "131,072"
    # board and connection, the capabilities, the time of the samples: all as text, copied at a click
    text = card.details_text()
    for part in ("Instrument", "Address: sim:pico", "Capabilities", "Reported:", "Time of the samples",
                 "Placed by:", "This computer's clock: free running", "Sample clock: Own", "Capture limits (samples)", "8 channels:"):
        assert part in text, part
    card.copy_details()
    assert QApplication.clipboard().text() == text
    # a device that cannot capture has no capture functions on its card
    assert card.action_settings.isVisible() and card.action_reconnect.isVisible()
    assert not card.action_bootloader.isVisible() and not card.action_network.isVisible()


def test_profiles_load_from_the_device_card(shell):
    card = card_of(shell)
    assert card.profiles_button.isVisibleTo(card) and card.profiles_button.isEnabled()
    card._fill_profiles()
    entries = [action.text() for action in card.profiles_menu.actions() if action.text()]
    assert "&Save current settings as profile..." in entries and "Standard profiles" in entries
    assert "Open the profiles folder" in entries
    standard = next(action.menu() for action in card.profiles_menu.actions() if action.text() == "Standard profiles")
    usable = next(action for action in standard.actions() if action.isEnabled())
    before = card.capture_label.text()
    assert before.startswith("Next capture: ")
    usable.trigger()  # the settings of the next capture are those of the profile, the card says so
    settle()
    assert card.capture_label.text().startswith("Next capture: ") and card.capture_label.text() != before


def test_a_device_that_can_restart_is_restarted_from_its_card(shell, monkeypatch):
    card = card_of(shell)
    driver = card.controller.driver
    assert CAPABILITY_RESTART in driver.capabilities() and card.action_restart.isVisible()
    gpio = card.instrument.gpio
    gpio.write("GP16", 1)
    from openscilab.core.instrument import MODE_INPUT, MODE_OUTPUT

    assert gpio.mode("GP16") == MODE_OUTPUT
    asked = []
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: asked.append(args[2]) or False)
    assert not card.restart_device() and gpio.mode("GP16") == MODE_OUTPUT  # cancelled
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: True)
    assert card.restart_device() and "Restart Simulation: Pico?" in asked[0]
    assert gpio.mode("GP16") == MODE_INPUT  # as after power-up
    assert "restarted" in card.banner.label.text()
    # a device that does not report RESTART has no such button (the Pico firmware has no command yet)
    from openscilab.driver.base import AnalyzerDriverBase

    assert AnalyzerDriverBase.restart(driver) is False


def test_the_timing_tab_shows_the_time_of_the_samples_and_its_options(shell, monkeypatch):
    card = card_of(shell)
    card.tabs.setCurrentWidget(card.timing_page)
    settle()
    assert card.tabs.tabText(card.tabs.indexOf(card.timing_page)) == "Timing"
    assert "nothing captured yet" in card.timing_label.text()
    assert card.forget_latency_button.isHidden() and "None measured" in card.latency_label.text()
    # a measured latency of this device shows, and is forgotten here
    key = timing.calibration_key(card.instrument, "stream", 1e6)
    timing.store_latency(key, timing.Latency(420e-6, 100e-6, "loopback"))
    timing.store_latency(timing.calibration_key(open_simulated("uno"), "stream", 1e6), timing.Latency(1e-3))
    assert list(timing.latencies_of(card.instrument)) == ["stream at 1 MHz"]
    card._update_timing()
    assert "stream at 1 MHz" in card.latency_label.text() and "420" in card.latency_label.text()
    assert not card.forget_latency_button.isHidden()
    assert "Measured latencies: stream at 1 MHz: 420" in card.details_text()  # (also on Details, as text)
    preferences.update({"timing.host_clock": "ptp"})
    card._update_timing()
    assert "follows PTP" in card.timing_label.text() and "Device time stamps" in card.timing_label.text()
    assert card.forget_latencies() == 1 and timing.stored_latency(key) is None
    assert len(timing.latencies_of(open_simulated("uno"))) == 1  # (those of other devices stay)
    # the ways to do better: the settings and the example flows
    pages, examples = [], []
    card.settings_requested.disconnect()  # (the shell opens the settings and the examples: dialogs)
    card.example_requested.disconnect()
    card.settings_requested.connect(pages.append)
    card.example_requested.connect(examples.append)
    for button in (card.time_settings_button, card.align_button, card.calibrate_button):
        button.click()
    assert pages == ["Time"] and examples == ["13-time/01-align-instruments", "13-time/02-calibrate-latency"]
    from openscilab.lab import examples as library

    assert all(library.find(key) is not None for key in examples)
    # after a capture in a flow the method and its accuracy are there
    state = timing.TimingState(timing.METHOD_SIGNAL, 2e-6, reference="pico")
    timing.timing_of(card.instrument).last_state = state
    card._update_timing()
    assert "sync signal" in card.timing_label.text() and "Reference" in card.timing_label.text()


# ------------------------------------------------------------------ device list
def test_every_simulated_device_is_in_the_group_of_the_simulators(shell):
    from openscilab.ui.shell.main_window import SIMULATOR_GROUP

    section = shell.devices_section
    preferences.update({"devices.simulators_open": True})
    section.refresh()
    rows = [section.list.item(row) for row in range(section.list.count())]
    group = next(index for index, item in enumerate(rows) if item.data(Qt.UserRole) == SIMULATOR_GROUP)
    before = [item.text() for item in rows[:group]]
    inside = [item.data(Qt.UserRole) for item in rows[group + 1:]]
    assert not any("Simulat" in text or "firmware protocol" in text for text in before), before
    assert all(entry.simulated for entry in inside)
    backends = {entry.backend for entry in inside}
    assert {"sim", "arduino", "rigol"} <= backends  # (also those that speak the real protocols)
    assert rows[group].text() == f"Simulators ({len(inside)})"
    preferences.update({"devices.show_simulators": False})
    section.refresh()
    texts = [section.list.item(row).text() for row in range(section.list.count())]
    assert not any("Simulat" in text or "DHO924S with" in text for text in texts)


def test_all_devices_disconnect_at_once_after_a_warning(shell, monkeypatch):
    section = shell.devices_section
    assert section.disconnect_all_button.isHidden() and not shell.disconnect_all()
    first, second = open_simulated("pico"), open_simulated("uno")
    shell.hub.add(first)
    shell.hub.add(second)
    settle()
    assert not section.disconnect_all_button.isHidden()
    first.gpio.write("GP16", 1)
    asked = []
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: asked.append(args[1:5]) or False)
    section.disconnect_all_button.click()
    assert len(shell.hub) == 2  # cancelled
    title, text, action, details = asked[0]
    assert title == "Disconnect all devices" and text == "Disconnect all 2 devices?" and action == "Disconnect all"
    assert "Simulation: Pico: it drives outputs" in details and "Simulation: Arduino Uno" in details
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: True)
    shell._fill_device_menus()
    assert shell.action_disconnect_all.isEnabled()
    shell.action_disconnect_all.trigger()
    settle()
    assert len(shell.hub) == 0 and section.disconnect_all_button.isHidden()


# ------------------------------------------------------------------- data views
def test_a_view_of_a_flows_data_does_not_capture(shell):
    import numpy as np

    from openscilab.driver.models import AnalyzerChannel, CaptureSession

    view = shell.new_data_view()
    shown = [item.key for item in view.main_toolbar._entries]
    assert "capture" in shown and "device" in shown and not view.viewer
    session = CaptureSession(frequency=1000, pre_trigger_samples=0, post_trigger_samples=100)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.zeros(100, np.uint8))]
    flow = shell.new_flow()
    data = shell.show_run_data(flow, "run", "Flow - run data", session)
    shown = [item.key for item in data.main_toolbar._entries if not item.stretch]
    assert data.viewer and shown == ["search", "measure", "listing", "panels", "save"]
    assert not data.runnable and not data.capture() and not data.action_repeat.isVisible()
    # a scope node's view as well
    from openscilab.core.signals import Capture

    scope = shell.show_view(flow, "scope", "scope", Capture(rate=1000.0, digital={"D0": np.zeros(50, np.uint8)}), {})
    assert scope.viewer and "capture" not in [item.key for item in scope.main_toolbar._entries]
    # the views of a device capture as before; a viewer given a device captures from then on
    instrument = open_simulated("pico")
    shell.hub.add(instrument)
    scope.use_instrument(instrument)
    assert not scope.viewer and scope.runnable
    assert "capture" in [item.key for item in scope.main_toolbar._entries]


# -------------------------------------------------------------------- hardware
def test_the_connected_hardware_says_how_firmware_gets_onto_a_board(shell):
    from openscilab.core import firmware
    from openscilab.ui.devices import hardware
    from openscilab.ui.documents.hardware import GUIDE, HardwareDocument

    for profile in ("pico", "uno"):
        shell.hub.add(open_simulated(profile))
    from openscilab.core.instrument import Instrument
    from openscilab.driver.simulated.arduino_shell import open_shell

    simulated_board = Instrument.from_driver(open_shell("uno"), uri="arduino-sim:uno")
    shell.hub.add(simulated_board)
    assert simulated_board.is_simulated
    entries = hardware.scan(shell.hub, {}, find_drives=list, detect=list, detect_foreign=list, images=[],
                            other_entries=list)
    assert entries == []  # simulated devices are no hardware
    document = HardwareDocument(shell.hub, scan=lambda hub, versions, images=None: [])
    try:
        assert "BOOTSEL" in GUIDE and "Install firmware" in GUIDE and document.guide_label.text() == GUIDE
        assert "Simulated devices are not listed" in document.table.item(0, 0).text()
        # a device whose firmware a tool writes (an Arduino): Update firmware in its row
        entry = hardware.HardwareEntry(key="open:uno", kind=hardware.DEVICE, title="Arduino", connection="arduino:x",
                                       instrument=simulated_board, method=firmware.UPDATE_METHODS["avrdude"])
        plain = hardware.HardwareEntry(key="open:scope", kind=hardware.DEVICE, title="Scope", connection="rigol:x",
                                       instrument=simulated_board)
        requested = []
        document.firmware_requested.connect(requested.append)
        from PySide6.QtWidgets import QLabel, QPushButton

        buttons = {button.text(): button for button in document._actions(entry).findChildren(QPushButton)}
        buttons["Update firmware..."].click()
        assert requested == [simulated_board]
        notes = [label.text() for label in document._actions(plain).findChildren(QLabel)]
        assert notes == ["no firmware to install"]
    finally:
        document.shutdown()


# ------------------------------------------------------------------------ tabs
def test_tabs_look_alike_and_have_no_base_line(shell):
    from openscilab.ui.theme import build_stylesheet

    sheet = build_stylesheet()
    assert "qproperty-drawBase: 0" in sheet and "QTabWidget#side-tabs" not in sheet
    assert "QTabWidget#document-group > QTabBar::tab" in sheet  # (only the documents have boxed tabs)
    card = card_of(shell)
    shell.show_console("Problems")
    settle()
    assert not shell.console.tabBar().drawBase() and not card.tabs.tabBar().drawBase()
