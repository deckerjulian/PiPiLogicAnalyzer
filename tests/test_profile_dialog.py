"""Editing profiles: the dialog, the stand-in driver and the Profiles menu."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.core.profiles import Profile, ProfileStore
from pipilogicanalyzer.driver.base import AnalyzerDriverType
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession
from pipilogicanalyzer.ui.dialogs import profile_dialog
from pipilogicanalyzer.ui.dialogs.profile_dialog import ProfileEditDialog, ProfileSettingsDriver


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def settings(*numbers: int, frequency: int = 1_000_000) -> CaptureSession:
    session = CaptureSession(frequency=frequency, pre_trigger_samples=100, post_trigger_samples=900)
    session.capture_channels = [AnalyzerChannel(channel_number=number, channel_name=f"CH{number}") for number in numbers]
    return session


def test_notes_are_stored_with_the_profile():
    profile = Profile("Bus", settings(0), [{"decoder_id": "i2c"}], notes="SDA on pin 2")
    assert Profile.from_dict(profile.to_dict()).notes == "SDA on pin 2"
    assert "Notes" not in Profile("Plain", settings(0)).to_dict()


def test_the_stand_in_driver_follows_the_channels():
    assert ProfileSettingsDriver(settings(0, 5)).driver_type == AnalyzerDriverType.SERIAL
    multi = ProfileSettingsDriver(settings(0, 30))
    assert multi.driver_type == AnalyzerDriverType.MULTI and multi.channel_count == 48
    assert multi.channels_per_device == 24
    assert multi.pattern_trigger_groups() == ((0, 21), (21, 3), (24, 21), (45, 3))


def test_the_capture_dialog_opens_for_a_multi_device_profile(application, monkeypatch):
    from pipilogicanalyzer.core import settings as settings_store
    from pipilogicanalyzer.ui.dialogs.capture_dialog import CaptureDialog

    monkeypatch.setattr(settings_store, "get_settings", lambda *args, **kwargs: None)
    dialog = CaptureDialog(ProfileSettingsDriver(settings(0, 30)))
    try:
        box = dialog.trigger_channel_box
        texts = [box.itemText(index) for index in range(box.count())]
        assert texts[24] == "Channel 25 (board 2)"
        assert texts[-1] == "External trigger (every board)"

        dialog.apply_session(settings(0, 30))
        dialog.pattern_radio.setChecked(True)
        dialog.pattern_base_box.setValue(31)
        dialog.pattern_edit.setText("101")
        session = dialog.build_session()
        assert session is not None and session.trigger_channel == 30
    finally:
        dialog.close()


def test_the_dialog_edits_name_notes_and_decoders(application):
    original = Profile("Bus", settings(0, 1), [{"decoder_id": "i2c", "label": "I2C"}], notes="old")
    dialog = ProfileEditDialog(original, [{"decoder_id": "uart", "label": "UART"}], ["Other"])
    assert "2 channels" in dialog.settings_label.text()
    assert dialog.decoder_list.item(0).text() == "I2C"

    dialog.name_edit.setText("Other")
    dialog._accept()
    assert dialog.profile is None and "already exists" in dialog.message.text()

    dialog.name_edit.setText("Renamed")
    dialog.notes_edit.setPlainText("new notes")
    dialog.use_current_decoders()
    assert dialog.decoder_list.item(0).text() == "UART"
    dialog._accept()

    assert dialog.profile.name == "Renamed" and dialog.profile.notes == "new notes"
    assert dialog.profile.decoder_configuration == [{"decoder_id": "uart", "label": "UART"}]
    assert original.name == "Bus" and original.decoder_configuration[0]["decoder_id"] == "i2c"


def test_the_capture_settings_are_edited_without_touching_the_defaults(application, monkeypatch):
    created = []

    class FakeCaptureDialog:
        def __init__(self, driver, parent=None, **kwargs):
            created.append((driver, kwargs))
            self.selected_settings = None

        def setWindowTitle(self, title):
            pass

        def apply_session(self, session):
            self.selected_settings = settings(0, 1, 2, frequency=20_000_000)

        def exec(self):
            return True

    monkeypatch.setattr(profile_dialog, "CaptureDialog", FakeCaptureDialog)
    dialog = ProfileEditDialog(Profile("Bus", settings(0, 1)), [], [])

    dialog.edit_capture_settings()

    _driver, kwargs = created[0]
    assert kwargs["persist"] is False and kwargs["accept_text"] == "Apply"
    assert "3 channels" in dialog.settings_label.text() and "20 MHz" in dialog.settings_label.text()

    dialog.clear_capture_settings()
    dialog.clear_decoders()
    dialog._accept()
    assert dialog.profile is None and "needs capture settings or decoders" in dialog.message.text()


def test_the_profiles_menu_edits_a_profile(application, monkeypatch):
    from pipilogicanalyzer.ui import main_window as main_window_module
    from pipilogicanalyzer.ui.main_window import MainWindow

    class FakeEditDialog:
        def __init__(self, profile, current_decoders, other_names, parent=None):
            self.profile = Profile("Renamed", profile.capture_settings, [], notes="edited")

        def exec(self):
            return True

    monkeypatch.setattr(main_window_module, "ProfileEditDialog", FakeEditDialog)
    window = MainWindow()
    try:
        window.profiles.add(Profile("Bus", settings(0)))
        window._rebuild_profiles_menu()
        (submenu,) = [action.menu() for action in window.profiles_menu.actions() if action.text() == "Bus"]
        assert [action.text() for action in submenu.actions() if action.text()] == ["Load", "Edit...", "Delete..."]

        window.edit_profile(window.profiles.get("Bus"))

        stored = ProfileStore()
        assert stored.get("Bus") is None and stored.get("Renamed").notes == "edited"
    finally:
        window.close()
