"""Fixed mistakes around data views, device cards and the shell (found in the UI review)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from openscilab.core import capture_io, recent, settings
from openscilab.core.instrument import InstrumentStatus
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated
from openscilab.sigrok.provider import DecoderInstance
from openscilab.ui import messages
from openscilab.ui.documents.dataview import DataView
from openscilab.ui.documents.device import COL_ACTION, COL_PIN


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def small_session() -> CaptureSession:
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=10, post_trigger_samples=90)
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="CLK",
                                                samples=(np.arange(100) % 2).astype(np.uint8))]
    return session


@pytest.fixture
def card(shell):
    instrument = open_simulated("free")
    shell.hub.add(instrument)
    return shell.open_device_card(instrument)


# ---------------------------------------------------------------- data views
def test_every_data_view_has_its_own_decoders(shell):
    first, second = shell.new_data_view(), shell.new_data_view()
    first.provider.add_instance(DecoderInstance("uart"))
    assert second.provider.instances == [] and first.provider.registry is second.provider.registry


def test_the_template_data_view_gets_the_capture(shell, card):
    template = shell.open_example("00-start/02-logic-analyzer")
    shell.area.activate(card)
    assert shell.show_data(card.instrument) is template
    assert len([doc for doc in shell.area.documents() if isinstance(doc, DataView)]) == 1


def test_unsaved_data_are_not_replaced_without_asking(shell, tmp_path, monkeypatch):
    path = str(tmp_path / "other.lac")
    capture_io.save_capture(path, small_session())
    view = shell.new_data_view()
    view.load_session(small_session())
    view._dirty = True  # e.g. a capture not saved yet
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: None)  # cancel
    view.open_capture_file(path)
    assert view.path is None
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 1)  # discard
    view.open_capture_file(path)
    assert view.path == path and not view.dirty


def test_names_markers_and_regions_are_unsaved_changes(shell):
    view = shell.new_data_view()
    view.load_session(small_session())
    view._dirty = False
    QApplication.processEvents()
    assert not view.dirty
    view.model.session.capture_channels[0].channel_name = "SCL"
    view.model.notify_channels_changed()
    assert view.dirty and view.has_edits
    view._set_dirty(False)
    view.model.add_bookmark(5, "here")
    assert view.dirty


def capture_view(shell, card):
    """The data view of the card's device, capturing once (capturing happens in the data view)."""
    view = shell.show_data(card.instrument)
    assert view.capture()
    return view


def test_a_capture_does_not_replace_edited_data(shell, card):
    view = capture_view(shell, card)
    assert wait_for(lambda: view.model.session is not None and not card.controller.is_capturing)
    QApplication.processEvents()
    view.model.add_bookmark(3, "mine")  # the user works with the data
    assert view.capture()
    assert card.controller.view is not view and view.source is None  # the next capture went elsewhere
    assert any(bookmark.name == "mine" for bookmark in view.model.bookmarks)
    assert wait_for(lambda: not card.controller.is_capturing)


def test_capture_again_uses_the_settings_not_the_edited_samples(shell, card):
    view = capture_view(shell, card)
    assert wait_for(lambda: view.model.session is not None and not card.controller.is_capturing)
    captured = card.controller.last_session
    view.model.session.post_trigger_samples = 3  # as deleting samples does
    started = []
    card.controller.capture = lambda session: started.append(session) or True
    assert card.controller.repeat_capture()
    assert started[0].post_trigger_samples == captured.post_trigger_samples


def test_a_closed_view_does_not_lose_the_running_capture(shell, card):
    view = capture_view(shell, card)
    shell.area.close_document(view, force=True)
    assert wait_for(lambda: not card.controller.is_capturing)
    assert wait_for(lambda: card.controller.view is not None and card.controller.view.model.session is not None)


# --------------------------------------------------------------- device card
def test_the_data_view_shows_the_next_capture_and_simulators_have_the_bar(shell, card):
    bar = shell.show_data(card.instrument).capture_controls.quick_capture
    assert bar.rate_combo.isEnabled()  # simulators too
    rate = bar.rate_combo
    rate.setCurrentIndex(0 if rate.currentIndex() else 1)
    session = bar.capture_session()
    assert card.controller.settings().frequency == session.frequency


def test_a_profile_sets_the_next_capture_of_this_device_only(card, shell):
    from openscilab.core.profiles import Profile
    from openscilab.ui.dialogs.capture_dialog import capture_settings_file

    session = CaptureSession(frequency=250_000, pre_trigger_samples=0, post_trigger_samples=500,
                             trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=2, channel_name="DATA")]
    other_file = "capture-settings-serial.json"
    view = shell.show_data(card.instrument)
    views = [doc for doc in shell.area.documents() if isinstance(doc, DataView)]
    assert card.controller.load_profile(Profile(name="slow", capture_settings=session, decoder_configuration=[]),
                                        card)
    assert settings.get_settings(capture_settings_file(card.controller.driver))["Frequency"] == 250_000
    assert settings.get_settings(other_file) is None
    assert view.capture_controls.quick_capture.capture_session().frequency == 250_000  # the bar read it again
    assert [doc for doc in shell.area.documents() if isinstance(doc, DataView)] == views  # no decoders: no new view


def test_a_profile_opens_no_data_view_its_decoders_wait_for_the_next(card, shell):
    from openscilab.core.profiles import Profile

    decoders = [{"decoder_id": "uart", "channel_map": {0: 0}}]
    assert card.controller.view is None
    assert card.controller.load_profile(Profile(name="uart", decoder_configuration=decoders), card)
    assert not [doc for doc in shell.area.documents() if isinstance(doc, DataView)]
    assert "next data view" in shell.statusBar().currentMessage()
    view = shell.show_data(card.instrument)
    assert [instance.decoder_id for instance in view.provider.instances] == ["uart"]
    assert card.controller.pending_decoders is None


def test_a_status_change_keeps_the_pin_controls(shell, monkeypatch):
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: True)
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    row = next(row for row in range(card.pin_table.rowCount()) if card.pin_table.item(row, COL_PIN).text() == "D7")
    switch = card.pin_table.cellWidget(row, COL_ACTION).findChild(QPushButton, "switch-D7")
    switch.click()
    assert switch.isChecked()
    shell.hub.set_status(instrument.name, InstrumentStatus.BUSY)
    assert card.pin_table.cellWidget(row, COL_ACTION).findChild(QPushButton, "switch-D7") is switch
    assert switch.isChecked()


def test_the_monitor_box_follows_the_monitor(shell):
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    view = shell.record_monitor(instrument)
    assert card.monitor_box.isChecked() and card._remove_handler is not None
    view.stop_recording()
    card.sync_monitor()
    assert not card.monitor_box.isChecked() and card._remove_handler is None


# ------------------------------------------------------------------ devices
def test_a_device_found_by_autodetect_has_its_real_address(shell, monkeypatch):
    from openscilab.ui import devices
    from openscilab.ui.devices import DeviceEntry

    driver = open_simulated("free").simulated_driver
    monkeypatch.setattr(type(driver), "address", property(lambda self: "pico:/dev/cu.usbmodem7"))
    backend = devices.DeviceBackend.__new__(devices.DeviceBackend)
    backend.connect = lambda entry, parent: driver
    instrument = devices.DeviceBackend.open_instrument(backend, DeviceEntry("pico", "autodetect", None, "Auto"),
                                                        shell)
    assert instrument.uri == "pico:/dev/cu.usbmodem7"


def test_an_unplugged_device_is_marked(shell):
    instrument = open_simulated("free")
    instrument.uri = "pico:/dev/cu.usbmodem9"
    shell.hub.add(instrument)
    shell.watch_ports(frozenset({"/dev/cu.usbmodem9"}))
    assert instrument.status != InstrumentStatus.DISCONNECTED
    shell.watch_ports(frozenset())
    assert instrument.status == InstrumentStatus.DISCONNECTED


# -------------------------------------------------------- shell and projects
def test_a_temporary_project_is_not_lost_and_not_recent(shell, monkeypatch):
    document = shell.open_example("00-start/01-empty-lab")
    assert document is not None and shell.is_temporary(document)
    assert not any(shell.in_temporary_project(path) for path in recent.entries())
    assert shell.saved_label.text().endswith("unsaved")
    asked = []
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: asked.append(args[2]) or None)
    documents = [doc for doc in shell.area.documents() if shell.is_temporary(doc)]
    for doc in documents[:-1]:
        assert shell.area.close_document(doc)  # others of the project are still open
    assert not asked
    assert not shell.area.close_document(documents[-1])  # the last one asks; cancelled
    assert asked and "temporary" in asked[0]


def test_save_as_in_a_temporary_project_keeps_the_project_first(shell, tmp_path, monkeypatch):
    document = shell.open_example("05-measurement/06-characteristic-curve")
    flow_document = next(doc for doc in shell.area.documents() if doc.document_kind == "flow")
    shell.area.activate(flow_document)
    answers = iter([(str(tmp_path / "Kept"), ""), (str(tmp_path / "Kept" / "flows" / "copy.flow.yaml"), "")])
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: next(answers))
    assert shell.save_active_as()
    assert not shell.is_temporary(document) and flow_document.path.endswith("copy.flow.yaml")


def test_quitting_asks_once_and_cancel_closes_nothing(shell, monkeypatch):
    first, second = shell.new_flow(), shell.new_flow()
    for document in (first, second):
        document.add_node("control.timer", (0, 0))
    asked = []
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: asked.append(args[3]) or None)
    assert not shell.confirm_quit()
    assert len(asked) == 1 and asked[0][0] == "Save all"
    assert first in shell.area.documents() and second in shell.area.documents()


def test_the_status_bar_shows_the_time_of_the_flow(shell):
    document = shell.new_flow()
    assert shell.mode_label.text() == "Real time" and not shell.mode_label.isHidden()
    document.fast_box.setChecked(True)
    assert shell.mode_label.text() == "Virtual time"
    shell.show_start_page()
    assert shell.mode_label.isHidden()
