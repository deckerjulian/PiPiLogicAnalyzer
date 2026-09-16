"""Capture dialog logic (channel selection, trigger validation, persistence)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from pipilogicanalyzer.core import settings
from pipilogicanalyzer.driver.emulated import EmulatedAnalyzerDriver
from pipilogicanalyzer.driver.models import TriggerType
from pipilogicanalyzer.ui.dialogs.capture_dialog import CaptureDialog


class FakeDriver(EmulatedAnalyzerDriver):
    """An emulated driver that pretends to be a real serial device."""

    def __init__(self) -> None:
        super().__init__(1)

    @property
    def driver_type(self):
        from pipilogicanalyzer.driver.base import AnalyzerDriverType

        return AnalyzerDriverType.SERIAL

    @property
    def blast_frequency(self) -> int:
        return 200_000_000

    @property
    def device_version(self) -> str:
        return "LOGIC_ANALYZER_V6_0"


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(application, monkeypatch):
    # Never block on a message box during the tests.
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    instance = CaptureDialog(FakeDriver())
    yield instance
    instance.close()


def select(dialog: CaptureDialog, channels: list[int]) -> None:
    for selector in dialog.channel_selectors:
        selector.enabled = selector.channel_number in channels


def test_no_channel_selected_is_rejected(dialog):
    select(dialog, [])
    dialog._accept()
    assert dialog.selected_settings is None


def test_edge_trigger_session(dialog):
    select(dialog, [0, 1, 2])
    dialog.edge_radio.setChecked(True)
    dialog.trigger_channel_box.setCurrentIndex(1)
    dialog.negative_trigger_box.setChecked(True)
    dialog.frequency_box.setValue(10_000_000)
    dialog.pre_samples_box.setValue(100)
    dialog.post_samples_box.setValue(900)

    dialog._accept()

    session = dialog.selected_settings
    assert session is not None
    assert [c.channel_number for c in session.capture_channels] == [0, 1, 2]
    assert session.trigger_type is TriggerType.EDGE
    assert session.trigger_channel == 1
    assert session.trigger_inverted
    assert session.total_samples == 1000


def test_burst_mode_sets_the_loop_count(dialog):
    select(dialog, [0])
    dialog.burst_box.setChecked(True)
    dialog.burst_count_box.setValue(4)
    dialog.measure_box.setChecked(True)
    dialog.post_samples_box.setValue(500)

    dialog._accept()

    session = dialog.selected_settings
    assert session.loop_count == 3
    assert session.measure_bursts
    assert session.total_samples == session.pre_trigger_samples + 500 * 4


def test_pattern_trigger_session(dialog):
    select(dialog, [0, 1])
    dialog.pattern_radio.setChecked(True)
    dialog.pattern_base_box.setValue(3)
    dialog.pattern_edit.setText("1011")

    dialog._accept()

    session = dialog.selected_settings
    assert session.trigger_type is TriggerType.COMPLEX
    assert session.trigger_channel == 2
    assert session.trigger_bit_count == 4
    assert session.trigger_pattern == 0b1101  # first character is the lowest bit


def test_pattern_must_fit_in_the_trigger_channels(dialog):
    select(dialog, [0])
    dialog.pattern_radio.setChecked(True)
    dialog.pattern_base_box.setValue(14)
    dialog.pattern_edit.setText("1010")  # 13 + 4 > 16

    dialog._accept()
    assert dialog.selected_settings is None


def test_fast_pattern_is_limited_to_five_bits(dialog):
    select(dialog, [0])
    dialog.pattern_radio.setChecked(True)
    dialog.fast_trigger_box.setChecked(True)

    # The editor caps the pattern at five characters...
    dialog.pattern_edit.setText("101010")
    assert dialog.pattern_edit.text() == "10101"

    # ...and the pattern still has to fit into the trigger inputs (channels 1 to 16 here).
    dialog.pattern_base_box.setValue(13)
    dialog._accept()
    assert dialog.selected_settings is None

    dialog.pattern_base_box.setValue(1)
    dialog._accept()
    assert dialog.selected_settings is not None
    assert dialog.selected_settings.trigger_type is TriggerType.FAST


def test_pattern_only_accepts_zeroes_and_ones(dialog):
    select(dialog, [0])
    dialog.pattern_radio.setChecked(True)
    dialog.pattern_edit.setText("10x1")

    dialog._accept()
    assert dialog.selected_settings is None


def test_blast_mode_forces_the_blast_frequency(dialog):
    select(dialog, [0])
    dialog.blast_box.setChecked(True)

    assert dialog.frequency_box.value() == 200_000_000
    assert dialog.pre_samples_box.value() == 0
    assert not dialog.burst_box.isEnabled()

    dialog._accept()
    session = dialog.selected_settings
    assert session.trigger_type is TriggerType.BLAST
    assert session.pre_trigger_samples == 0


def test_settings_are_persisted_and_restored(dialog, application):
    select(dialog, [2, 5])
    dialog.channel_selectors[2].channel_name = "SCL"
    dialog.frequency_box.setValue(4_000_000)
    dialog.post_samples_box.setValue(2048)
    dialog._accept()

    assert settings.get_settings(dialog.settings_file) is not None

    restored = CaptureDialog(FakeDriver())
    try:
        assert [s.channel_number for s in restored.channel_selectors if s.enabled] == [2, 5]
        assert restored.channel_selectors[2].channel_name == "SCL"
        assert restored.frequency_box.value() == 4_000_000
        assert restored.post_samples_box.value() == 2048
    finally:
        restored.close()


def test_row_buttons_select_and_invert(dialog):
    select(dialog, [])
    dialog._set_row(0, True)
    assert dialog.enabled_channels() == list(range(8))

    dialog._set_row(0, None)
    assert dialog.enabled_channels() == []


def test_limits_follow_the_selected_channels(dialog):
    select(dialog, [0])
    eight_bit = dialog.post_samples_box.maximum()

    select(dialog, [0, 20])
    dialog._update_limits()
    assert dialog.post_samples_box.maximum() < eight_bit
