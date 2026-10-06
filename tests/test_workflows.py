"""The ways through the application: first start, connecting, capturing, feedback, projects, tabs."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QLineEdit

from openscilab.core import recent
from openscilab.core.instrument import InstrumentStatus
from openscilab.driver.base import DeviceConnectionError
from openscilab.driver.simulated import open_simulated
from openscilab.ui import messages
from openscilab.ui.documents.device import DeviceDocument
from openscilab.ui.documents.flow import FlowDocument


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ------------------------------------------------------------- first start
def test_the_empty_area_says_what_to_do(shell):
    shell.close_all_documents(force=True)
    assert shell.area.stack.currentWidget() is shell.area.empty_page
    shell.area.empty_buttons["flow"].click()
    assert isinstance(shell.active_document(), FlowDocument)
    assert shell.area.stack.currentWidget() is shell.area.root


def test_the_start_page_connects_a_simulator(shell):
    page = shell.show_start_page()
    titles = [action.text() for action in page.simulator_menu.actions()]
    assert "Simulation: Arduino Uno" in titles
    page.simulator_requested.emit("uno")
    card = shell.active_document()
    assert isinstance(card, DeviceDocument) and card.instrument.uri == "sim:uno"
    page.simulator_requested.emit("uno")  # once is enough: the card again
    assert len(shell.hub) == 1


def test_connect_lists_boards_by_hand_and_simulators(shell, monkeypatch):
    from openscilab.ui.dialogs.connect_dialog import ConnectDialog

    dialog = ConnectDialog(shell)
    texts = [dialog.list.item(row).text() for row in range(dialog.list.count())]
    assert "Simulators" in texts and any("Arduino Uno" in text for text in texts)
    assert any("Arduino Uno (firmware protocol)" in text for text in texts)
    assert dialog.connect_button.isEnabled()

    def choose(self):
        row = next(row for row in range(self.list.count()) if "Arduino Uno" in self.list.item(row).text()
                   and "R4" not in self.list.item(row).text() and "protocol" not in self.list.item(row).text())
        self.list.setCurrentRow(row)
        self._accept()
        return True

    monkeypatch.setattr(ConnectDialog, "exec", choose)
    shell.action_connect.trigger()
    assert [instrument.uri for instrument in shell.hub.instruments()] == ["sim:uno"]
    shell._fill_device_menus()
    assert [action.text() for action in shell.card_menu.actions()] == [shell.hub.instruments()[0].name]


def test_a_failed_connection_shows_in_the_device_list(shell, monkeypatch):
    from openscilab.ui.devices import pico as pico_devices

    def fail(address, download_bitstream=False):
        raise DeviceConnectionError("port busy")

    from openscilab.driver import discovery

    monkeypatch.setattr(discovery, "open_device", fail)
    assert shell.connect_entry(pico_devices.serial_entry("/dev/cu.test")) is None
    banner = shell.devices_section.error_banner
    assert not banner.isHidden() and "port busy" in banner.label.text()


# -------------------------------------------------------------- devices
def test_disconnecting_while_capturing_asks(shell, monkeypatch):
    instrument = open_simulated("free")
    shell.hub.add(instrument)
    monkeypatch.setattr(instrument.capture.driver.__class__, "is_capturing", property(lambda self: True))
    asked = []
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: asked.append(args[3]) or False)
    assert not shell.disconnect_instrument(instrument)
    assert instrument in shell.hub and asked == ["Disconnect"]


def test_reconnect_after_unplugging(shell):
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    shell.hub.set_status(instrument.name, InstrumentStatus.DISCONNECTED, "unplugged")
    assert not card.reconnect_button.isHidden() and card.disconnect_button.isHidden()
    assert "unplugged" in card.banner.label.text()
    card.reconnect_button.click()
    QApplication.processEvents()
    new_card = shell.active_document()
    assert isinstance(new_card, DeviceDocument) and new_card is not card
    assert new_card.instrument.uri == "sim:uno" and new_card.instrument in shell.hub
    assert card not in shell.area.documents()


# -------------------------------------------------------------- capture
def test_the_header_captures_in_the_data_view(shell):
    instrument = open_simulated("free")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    assert not shell.run_button.isEnabled()  # capturing happens in the data view, not on the card
    view = shell.show_data(instrument)
    state = view.capture_controls.state_label
    assert shell.run_button.text() == "Capture" and shell.run_button.isEnabled()
    assert "Ready" in state.text()
    shell.run_button.click()
    assert card.controller.is_capturing or card.controller.last_session is not None
    assert wait_for(lambda: "Last capture" in state.text())  # once the result arrived


def test_a_failed_capture_is_a_banner_not_a_dialog(shell):
    from openscilab.driver.base import CaptureCompletedArgs

    instrument = open_simulated("free")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    view = shell.show_data(instrument)
    card.controller._completed(CaptureCompletedArgs(success=False, session=None, error="timeout"))
    assert not view.capture_banner.isHidden() and "timeout" in view.capture_banner.label.text()
    assert "failed" in view.capture_controls.state_label.text()


def test_the_settings_dialog_uses_settings_and_enter_moves_on(shell):
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    instrument = open_simulated("free")
    dialog = CaptureDialog(instrument.capture.driver, shell, allow_apply=True)
    assert dialog.apply_button.text() == "Use settings" and dialog.profiles_button.isHidden()
    names = [edit for edit in dialog.findChildren(QLineEdit, "channel-name") if edit.isEnabled()]
    assert len(names) >= 2
    names[0].setFocus()
    dialog.show()
    QApplication.processEvents()
    names[0].setFocus()
    dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert dialog.result() == 0 and not dialog.isHidden()  # not accepted: no capture
    dialog.reject()


# ------------------------------------------------------------- feedback
def test_a_flow_that_cannot_run_says_why_above_the_graph(shell):
    document = shell.new_flow()
    document.add_node("data.table", (0, 0))  # its input is not wired
    if document.flow.validate(document.registry):
        assert not document.start_run()
        assert not document.run_banner.isHidden()
        assert shell.console_dock.isVisible()
    document.report_problems()
    assert not document.run_banner.isHidden()


def test_problems_are_counted_and_lead_to_their_node(shell):
    document = shell.new_flow()
    document.add_node("device.capture", (0, 0), "cap")  # needs a device
    document.report_problems()
    assert not shell.problems_button.isHidden()
    problem = next(problem for problem in shell.console.problems.problems() if problem.node == "cap")
    shell.show_problem(problem)
    assert [item.node_id for item in document.scene.selected_nodes()] == ["cap"]


def test_a_run_without_its_device_offers_the_simulators(shell):
    document = shell.new_flow()
    document._run_finished(type("Result", (), {"state": "error", "error": "device uno (pico:/dev/x): gone",
                                               "time": 0.0})())
    assert document.run_banner.button.text() == "Run with simulators"


# ------------------------------------------------------------- projects
def test_the_project_menu_offers_projects(shell, tmp_path):
    texts = [action.text() for action in shell.project_menu.actions()]
    assert "New pro&ject..." in texts and "Open pro&ject..." in texts
    assert shell.action_new_flow.shortcut() == QKeySequence(QKeySequence.New)  # Ctrl+N: a new flow
    document = shell.open_example("00-start/01-empty-lab", str(tmp_path / "lab"))
    assert document is not None
    shell._rebuild_recent_menu()
    titles = [action.text() for action in shell.recent_menu.actions()]
    assert "lab" in titles or any("lab" in title for title in titles)
    flow = next(doc for doc in shell.area.documents() if isinstance(doc, FlowDocument))
    shell.area.activate(flow)
    files = [shell.project_section.files.item(row).text() for row in range(shell.project_section.files.count())]
    assert any(name.endswith(".flow.yaml") for name in files) and any(name.endswith(".panel.yaml") for name in files)


def test_files_of_temporary_projects_are_not_recent(shell):
    shell.open_example("05-measurement/06-characteristic-curve")
    assert not any("openscilab-" in path for path in recent.entries("files"))


# ----------------------------------------------------------------- tabs
def test_untitled_documents_are_numbered_and_tabs_cycle(shell):
    first, second = shell.new_flow(), shell.new_flow()
    assert first.title.startswith("Untitled flow") and first.title != second.title
    shell.area.activate(second)
    shell.action_next_tab.trigger()
    assert shell.active_document() is not second


def test_split_needs_a_second_document(shell):
    shell.close_all_documents(force=True)
    document = shell.new_flow()
    shell.action_split_right.trigger()
    assert len(shell.area.groups()) == 1
    shell.new_flow()
    shell.action_split_right.trigger()
    assert len(shell.area.groups()) == 2
    assert document in shell.area.documents()


# ------------------------------------------------------------- polishing
def test_shortcuts_are_listed_with_the_keys_of_the_views(shell):
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    shell.open_device_card(instrument)
    dialog = shell.show_shortcuts()
    rows = dialog.visible_rows()
    assert any(title.endswith("› All outputs safe") and keys == "Esc (Pins tab)" for title, keys in rows)
    assert any(title.startswith("Flow graph ›") for title, _keys in rows)
    dialog.search.setText("pinch")
    assert dialog.visible_rows() and all("pinch" in f"{t} {k}".lower() for t, k in dialog.visible_rows())
    dialog.close()


def test_card_and_panel_commands_are_in_the_palette(shell):
    instrument = open_simulated("free")
    shell.hub.add(instrument)
    shell.open_device_card(instrument)
    titles = [command.title for command in shell.commands()]
    assert any(title.endswith("› Capture in a data view") for title in titles)
    shell.new_panel()
    titles = [command.title for command in shell.commands()]
    assert any(title.endswith("› Operate") for title in titles)


def test_bare_keys_act_only_in_the_data_view(make_dataview):
    view = make_dataview()
    zoom_in = next(action for action in view.findChildren(QAction) if action.text() == "Zoom &in")
    assert zoom_in.shortcutContext() == Qt.WidgetWithChildrenShortcut
    assert view.action_measure.shortcut().toString() == "Ctrl+Shift+M"


def test_detached_windows_have_the_shortcuts_and_menus(shell):
    document = shell.new_flow()
    window = shell.area.detach(document)
    texts = [action.text() for action in window.actions()]
    assert "&Save" in texts and "&Undo" in texts
    assert "F&low" in [action.text() for action in window.menuBar().actions()]
    window.close()
