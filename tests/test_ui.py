"""GUI tests, executed on the offscreen Qt platform."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PySide6.QtWidgets import QApplication

from openscilab.core.regions import SampleRegion
from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.ui.view_model import CaptureViewModel


@pytest.fixture(scope="session")
def application():
    app = QApplication.instance() or QApplication([])
    yield app


def square_wave(period: int, count: int) -> np.ndarray:
    return ((np.arange(count) // (period // 2)) % 2).astype(np.uint8)


def make_session(samples: int = 1000) -> CaptureSession:
    session = CaptureSession(
        frequency=1_000_000, pre_trigger_samples=200, post_trigger_samples=samples - 200
    )
    session.capture_channels = [
        AnalyzerChannel(channel_number=index, channel_name=f"CH{index + 1}",
                        samples=square_wave(2 ** (index + 2), samples))
        for index in range(4)
    ]
    return session


@pytest.fixture
def window(application, make_dataview):
    main = make_dataview()
    main.resize(1200, 800)
    main.load_session(make_session())
    yield main
    main.close()


# ----------------------------------------------------------------- view model
def test_view_is_clamped_to_the_capture():
    model = CaptureViewModel()
    model.set_session(make_session(500))
    model.set_view(-10, 10_000)
    assert model.first_sample == 0
    assert model.visible_samples == 500


def test_zoom_keeps_the_anchor_sample():
    model = CaptureViewModel()
    model.set_session(make_session(10_000))
    model.set_view(1000, 1000)
    model.zoom(0.5, anchor_sample=1500)
    assert model.first_sample <= 1500 <= model.first_sample + model.visible_samples


def test_regions_in_view_are_filtered():
    model = CaptureViewModel()
    model.set_session(make_session(1000))
    model.set_view(0, 100)
    model.add_regions(
        [SampleRegion(first_sample=10, last_sample=20), SampleRegion(first_sample=500, last_sample=600)]
    )
    assert len(model.regions_in_view()) == 1


def test_user_marker_is_bounded():
    model = CaptureViewModel()
    model.set_session(make_session(100))
    model.set_user_marker(50)
    assert model.user_marker == 50
    model.set_user_marker(5000)
    assert model.user_marker is None


# --------------------------------------------------------------- main window
def test_window_loads_a_capture(window):
    assert window.model.sample_count == 1000
    assert window.action_save.isEnabled()
    assert "Total samples" in window.info_label.text()


def test_widgets_paint_at_every_zoom_level(window):
    for visible in (10, 100, 1000):
        window.model.set_view(0, visible)
        window.sample_viewer.grab()
        window.sample_marker.grab()
        window.previewer.grab()


def test_copy_and_paste_samples(window):
    original = window.model.sample_count
    window.copy_samples(0, 100)
    assert window.clipboard_samples is not None

    window.paste_samples(0)
    assert window.model.sample_count == original + 100
    assert window.model.session.pre_trigger_samples == 300


def test_delete_samples_updates_the_trigger_and_the_regions(window):
    window.model.add_region(SampleRegion(first_sample=500, last_sample=600, region_name="after"))
    window.delete_samples(0, 100)

    assert window.model.sample_count == 900
    assert window.model.session.pre_trigger_samples == 100
    assert window.model.regions[0].start == 400


def test_delete_removes_regions_inside_the_range(window):
    window.model.add_region(SampleRegion(first_sample=10, last_sample=20))
    window.delete_samples(0, 100)
    assert window.model.regions == []


def test_delete_keeps_the_channels_consistent(window):
    window.delete_samples(10, 10)
    lengths = {channel.samples.size for channel in window.model.session.capture_channels}
    assert lengths == {990}


@pytest.mark.parametrize("operation", ["delete", "insert"])
def test_session_counters_match_the_samples_after_editing(window, operation):
    if operation == "delete":
        window.delete_samples(0, 100)
    else:
        window._insert_samples(0, [np.ones(50, dtype=np.uint8)] * 4)

    session = window.model.session
    assert session.total_samples == window.model.sample_count
    assert session.pre_trigger_samples + session.post_trigger_samples == window.model.sample_count


def test_insert_samples_shifts_the_regions(window):
    window.model.add_region(SampleRegion(first_sample=300, last_sample=400))
    window._insert_samples(100, [np.ones(50, dtype=np.uint8)] * 4)

    assert window.model.sample_count == 1050
    assert window.model.regions[0].start == 350


def test_shifting_channels_left_fills_with_the_selected_level(window, monkeypatch):
    from openscilab.ui.dialogs import shift_dialog

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            self.shifted_channels = [window.model.session.capture_channels[0]]
            self.shift_amount = 5
            self.direction = shift_dialog.ShiftDirection.LEFT
            self.mode = shift_dialog.ShiftMode.HIGH

        def exec(self):
            return True

    monkeypatch.setattr("openscilab.ui.documents.dataview.ShiftChannelsDialog", FakeDialog)
    before = window.model.session.capture_channels[0].samples.copy()
    window.shift_channels()
    after = window.model.session.capture_channels[0].samples

    assert after.size == before.size
    assert np.array_equal(after[:-5], before[5:])
    assert np.all(after[-5:] == 1)


def test_transitions_are_rebuilt_after_editing(window):
    window.delete_samples(0, 500)
    transitions = window.model.transitions_for(window.model.session.capture_channels[0])
    assert transitions.sample_count == 500


def test_scrollbar_follows_the_view(window):
    window.model.set_view(100, 200)
    assert window.position_scrollbar.value() == 100
    assert window.position_scrollbar.pageStep() == 200


def test_zoom_slider_roundtrip(window):
    window.model.set_view(0, 250)
    slider_value = window.zoom_slider.value()
    assert abs(window._slider_to_zoom(slider_value) - 250) <= 5


def test_saving_and_reopening_a_capture(window, tmp_path, monkeypatch):
    path = str(tmp_path / "capture.lac")
    monkeypatch.setattr(
        "openscilab.ui.documents.dataview.QFileDialog.getSaveFileName", lambda *a, **k: (path, "")
    )
    window.save_capture()
    assert os.path.exists(path)

    window.open_capture_file(path)
    assert window.model.sample_count == 1000


def test_keyboard_navigation_actions_exist(window):
    shortcuts = {
        action.shortcut().toString()
        for action in window.findChildren(type(window.action_save))
        if not action.shortcut().isEmpty()
    }
    assert {"Ctrl+Left", "Ctrl+Right", "Ctrl+Up", "Ctrl+Down", "Shift+Left", "Shift+Right"} <= shortcuts


def test_forget_known_devices_without_any(shell, monkeypatch):
    messages = []
    monkeypatch.setattr("openscilab.ui.messages.info", lambda *args, **kwargs: messages.append(args))
    shell.forget_known_devices()
    assert messages


def test_forget_known_devices_clears_the_file(shell, monkeypatch):
    from openscilab.core import settings

    settings.persist_settings("known-devices.json", [{"serial_numbers": ["a", "b"]}])
    monkeypatch.setattr("openscilab.ui.messages.confirm", lambda *args, **kwargs: True)
    assert shell.forget_known_devices()
    assert settings.get_settings("known-devices.json") == []


def test_export_writes_the_selected_format(window, tmp_path, monkeypatch):
    for kind, extension in (("csv", ".csv"), ("vcd", ".vcd")):
        path = str(tmp_path / f"export{extension}")
        monkeypatch.setattr(
            "openscilab.ui.documents.dataview.QFileDialog.getSaveFileName",
            lambda *a, **k: (path, ""),
        )
        window.export_capture(kind)
        assert os.path.getsize(path) > 0


def test_create_region_from_a_selection(window, monkeypatch):
    monkeypatch.setattr(
        "openscilab.ui.documents.dataview.RegionDialog",
        lambda region, parent=None: type("D", (), {"exec": lambda self: True})(),
    )
    window.create_region(10, 50)
    assert window.model.regions[0].start == 10
    assert window.model.regions[0].end == 50


def test_channels_with_identical_metadata_are_distinguished(window):
    """Regression: comparing channels by value raised on the numpy samples."""
    session = window.model.session
    for channel in session.capture_channels:
        channel.channel_number = 0
        channel.channel_name = "same"
    window.model.rebuild_transitions()

    first, last = session.capture_channels[0], session.capture_channels[-1]
    assert window.model.transitions_for(first) is not None
    assert window.model.transitions_for(first) is not window.model.transitions_for(last)
    window.sample_viewer.grab()


def test_window_geometry_is_restored(application):
    from openscilab.core import settings
    from openscilab.ui.shell.main_window import SHELL_STATE_FILE, ShellWindow

    first = ShellWindow()
    try:
        first.resize(1111, 777)
        first.save_state()
    finally:
        first.force_close()

    assert settings.get_settings(SHELL_STATE_FILE)["width"] == 1111

    second = ShellWindow()
    try:
        assert second.size().width() == 1111
        assert second.size().height() == 777
    finally:
        second.force_close()


def test_the_analyzer_dock_layout_is_restored(make_dataview):
    first = make_dataview()
    first.listing_dock.setVisible(True)
    first._save_window_state()

    second = make_dataview()
    assert not second.listing_dock.isHidden()


def test_the_help_menu_links_to_both_wikis(shell, monkeypatch):
    from openscilab.ui.shell import main_window as main_window_module

    opened = []
    monkeypatch.setattr(main_window_module.webbrowser, "open", lambda url: opened.append(url))

    actions = {action.text().replace("&", ""): action for action in shell.help_menu.actions()}
    actions["Online documentation"].trigger()
    actions["Online documentation of the original software (gusmanb)"].trigger()

    assert opened == [
        "https://github.com/deckerjulian/openSciLab/wiki",
        "https://github.com/gusmanb/logicanalyzer/wiki",
    ]
