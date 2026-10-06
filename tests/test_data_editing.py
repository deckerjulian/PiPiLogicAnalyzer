"""Editing data: undo and redo of sample edits and channel changes, the order of the channels,
hidden channels, regions, and channel names for the next capture."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from openscilab.core import capture_io
from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.driver.simulated import open_simulated
from openscilab.ui import messages


def session(samples: int = 1000, channels: int = 3) -> CaptureSession:
    capture = CaptureSession(frequency=1_000_000, pre_trigger_samples=100, post_trigger_samples=samples - 100)
    capture.capture_channels = [
        AnalyzerChannel(channel_number=number, channel_name=f"CH{number + 1}",
                        samples=((np.arange(samples) // (number + 2)) % 2).astype(np.uint8))
        for number in range(channels)]
    return capture


def loaded(make_dataview):
    view = make_dataview()
    view.load_session(session())
    QApplication.processEvents()
    return view


def samples_of(view) -> list[bytes]:
    return [channel.samples.tobytes() for channel in view.model.session.capture_channels]


def test_sample_edits_are_undone_and_redone(make_dataview):
    view = loaded(make_dataview)
    original = samples_of(view)
    view.delete_samples(10, 100)
    assert view.model.sample_count == 900 and view.dirty
    view._insert_samples(0, [np.ones(50, np.uint8)] * 3)
    assert view.model.sample_count == 950
    assert view.undo.count() == 2 and view.undo.undoText() == "Insert 50 samples"
    view.undo.undo()
    view.undo.undo()
    assert view.model.sample_count == 1000 and samples_of(view) == original
    assert view.model.session.pre_trigger_samples == 100
    view.undo.redo()
    assert view.model.sample_count == 900


def test_names_markers_and_order_are_undone(make_dataview):
    view = loaded(make_dataview)
    first = view.model.channels[0]
    first.channel_name = "CLK"
    view.model.notify_channels_changed()
    view.model.add_bookmark(5, "start")
    assert view.model.move_channel(first, 2)
    assert [channel.channel_number for channel in view.model.channels] == [1, 2, 0]
    view.undo.undo()
    assert [channel.channel_number for channel in view.model.channels] == [0, 1, 2]
    view.undo.undo()
    assert view.model.bookmarks == []
    view.undo.undo()
    assert first.channel_name == "CH1"


def test_the_shell_undoes_data_edits(shell, make_dataview):
    view = loaded(make_dataview)
    shell.area.activate(view)
    view.delete_samples(0, 10)
    shell._on_active_changed(view)
    assert shell.action_undo.isEnabled()
    shell.action_undo.trigger()
    assert view.model.sample_count == 1000


def test_the_order_of_channels_is_saved(make_dataview, tmp_path):
    view = loaded(make_dataview)
    view.model.move_channel(view.model.channels[2], 0)
    path = str(tmp_path / "order.lac")
    capture_io.save_capture(path, view.model.session, view.model.regions)
    again = capture_io.load_capture(path).session
    assert [channel.channel_number for channel in again.capture_channels] == [2, 0, 1]


def test_moving_by_the_menu_skips_hidden_channels(make_dataview):
    view = loaded(make_dataview)
    channels = view.model.channels
    channels[1].hidden = True
    view.model.notify_channels_changed()
    view.channel_viewer._move_by(channels[0], 1)  # past the hidden one, below CH3
    assert [channel.channel_number for channel in view.model.channels] == [1, 2, 0]


def test_hidden_channels_can_be_shown_one_by_one(make_dataview):
    view = loaded(make_dataview)
    channels = view.model.channels
    for channel in channels[:2]:
        channel.hidden = True
    view.model.notify_channels_changed()
    assert view.show_all_button.text() == "2 hidden" and view.show_all_button.isEnabled()
    view._fill_hidden_menu()
    texts = [action.text() for action in view.hidden_menu.actions()]
    assert texts[0] == "Show all channels" and "Show CH1" in texts
    next(action for action in view.hidden_menu.actions() if action.text() == "Show CH1").trigger()
    assert not channels[0].hidden and view.show_all_button.text() == "1 hidden"


def test_the_data_menu_edits_the_selection(make_dataview):
    from openscilab.ui.widgets.sample_marker import Selection

    view = loaded(make_dataview)
    view.sample_marker.selection = Selection(100, 199)
    copy, cut, paste, delete = (next(action for action in view.sample_actions if action.text() == text)
                                for text in ("&Copy samples", "Cu&t samples", "&Paste samples", "De&lete samples"))
    copy.trigger()
    assert view.clipboard_samples is not None and len(view.clipboard_samples[0]) == 100
    delete.trigger()
    assert view.model.sample_count == 900
    view.sample_marker.selection = None
    delete.trigger()  # nothing selected: says how
    assert "Select samples first" in view.statusBar().currentMessage()
    paste.trigger()
    assert view.model.sample_count == 1000


def test_inserting_works_without_a_device(make_dataview, monkeypatch):
    view = loaded(make_dataview)
    assert view.driver is None

    class Dialog:
        def __init__(self, *args, **kwargs):
            self.samples = [np.zeros(10, np.uint8)] * 3

        def exec(self):
            return True

    monkeypatch.setattr("openscilab.ui.documents.dataview.CreateSamplesDialog", Dialog)
    view.insert_samples_dialog(0)
    assert view.model.sample_count == 1010


def test_a_region_can_be_edited(make_dataview, monkeypatch):
    from openscilab.core.regions import SampleRegion
    from openscilab.ui.dialogs.region_dialog import RegionDialog

    view = loaded(make_dataview)
    view.model.add_region(SampleRegion(first_sample=10, last_sample=50, region_name="reset"))
    panel = view.markers_panel
    regions = panel.tree.topLevelItem(1)
    panel.tree.setCurrentItem(regions.child(0))
    regions.child(0).setSelected(True)

    def rename(dialog):
        dialog.name_edit.setText("boot")
        dialog._accept()
        return True

    monkeypatch.setattr(RegionDialog, "exec", rename)
    assert panel.edit_selected()
    assert view.model.regions[0].region_name == "boot"


def test_names_go_to_the_next_capture_of_the_device(shell, monkeypatch):
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: True)
    instrument = open_simulated("free")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    view = shell.show_data(instrument)
    capture = session(100, 2)
    view.load_session(capture)
    capture.capture_channels[0].channel_name = "SCL"
    assert view.use_names_for_device()
    assert card.controller.channel_names().get(0) == "SCL"
