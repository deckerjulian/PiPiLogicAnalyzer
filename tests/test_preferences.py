"""Settings: stored values, the dialog, what they change, the light theme."""

from __future__ import annotations

import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from openscilab.core import preferences, recent
from openscilab.ui import messages


def test_defaults_bad_values_and_storage(tmp_path):
    assert preferences.get("appearance.theme") == "dark" and preferences.get("navigation.wheel") == "zoom"
    assert preferences.update({"appearance.theme": "light", "devices.refresh_s": 3}) == {
        "appearance.theme": "light", "devices.refresh_s": 3.0}
    with pytest.raises(ValueError):
        preferences.update({"appearance.theme": "pink"})
    with pytest.raises(ValueError):
        preferences.update({"unknown": 1})
    preferences.reload()
    assert preferences.get("appearance.theme") == "light"  # read back from the file
    assert preferences.reset()["appearance.theme"] == "dark"
    assert preferences.default_folder().endswith(os.path.join("Documents", "openSciLab"))


def test_the_dialog_changes_the_preferences(shell, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from openscilab.ui.dialogs.preferences_dialog import PreferencesDialog

    sheets = []
    monkeypatch.setattr(QApplication.instance(), "setStyleSheet", sheets.append)

    def choose(self):
        self.font_size.setValue(14)
        self.wheel.setCurrentIndex(self.wheel.findData("scroll"))
        self.refresh.setValue(5)
        self.simulators.setChecked(False)
        self._accept()
        return True

    monkeypatch.setattr(PreferencesDialog, "exec", choose)
    changes = shell.show_preferences()
    assert changes == {"appearance.font_size": 14, "navigation.wheel": "scroll", "devices.refresh_s": 5.0,
                       "devices.show_simulators": False}
    assert "font-size: 14px" in shell.styleSheet()
    assert shell.port_timer.interval() == 5000
    assert not any(preset.kind == "simulator" for preset in shell.device_presets())
    assert shell.action_settings.menuRole() == shell.action_settings.MenuRole.PreferencesRole


def test_a_new_theme_offers_a_restart(shell, monkeypatch):
    asked = []
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: asked.append(args[3]) or 1)  # later
    shell.apply_preferences({"appearance.theme": "light"})
    assert asked == [["Restart now", "Later"]]


def test_the_wheel_can_scroll_instead_of_zooming(shell):
    document = shell.new_flow()
    document.add_node("data.table", (0, 0))
    view = document.view
    view.actual_size()
    preferences.update({"navigation.wheel": "scroll"})
    before = view.verticalScrollBar().value()
    position = QPointF(100, 100)
    view.wheelEvent(QWheelEvent(position, view.viewport().mapToGlobal(position), QPoint(0, 0), QPoint(0, -120),
                                Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
    assert view.zoom_level() == pytest.approx(1.0) and view.verticalScrollBar().value() > before
    view.wheelEvent(QWheelEvent(position, view.viewport().mapToGlobal(position), QPoint(0, 0), QPoint(0, 120),
                                Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False))
    assert view.zoom_level() > 1.0


def test_recent_lists_keep_as_many_as_set(tmp_path):
    preferences.update({"data.recent_count": 2})
    for name in ("a", "b", "c"):
        path = tmp_path / name
        path.write_text("x")
        recent.add(str(path))
    assert [os.path.basename(path) for path in recent.entries()] == ["c", "b"]


def test_the_light_theme(tmp_path):
    """A start with the light theme: light colours, the waveform display stays dark."""
    code = ("import openscilab.ui.theme as t; print(t.THEME_MODE, t.BACKGROUND, t.DISPLAY['BACKGROUND'], "
            "'#1c1c20' in t.build_stylesheet())")
    env = dict(os.environ, OPENSCILAB_THEME="light", OPENSCILAB_SETTINGS_DIR=str(tmp_path))
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                            cwd=os.path.dirname(os.path.dirname(__file__))).stdout.split()
    assert output == ["light", "#f5f5f7", "#303033", "True"]


def test_text_contrast_of_both_themes():
    from openscilab.ui.theme import PALETTES

    def luminance(color: str) -> float:
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(first: str, second: str) -> float:
        a, b = sorted((luminance(first), luminance(second)), reverse=True)
        return (a + 0.05) / (b + 0.05)

    for palette in PALETTES.values():
        for background in ("BACKGROUND", "PANEL", "INPUT"):
            assert contrast(palette["TEXT"], palette[background]) >= 7
            assert contrast(palette["TEXT_MUTED"], palette[background]) >= 4.5, background
