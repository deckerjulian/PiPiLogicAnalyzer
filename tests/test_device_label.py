"""The current device in the device bar links to the board information."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pipilogicanalyzer.driver.emulated import EmulatedAnalyzerDriver


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_clicking_the_current_device_opens_the_board_information(application, monkeypatch):
    from pipilogicanalyzer.ui import main_window as main_window_module
    from pipilogicanalyzer.ui.main_window import MainWindow

    opened = []

    class RecordingDialog:
        def __init__(self, driver, parent=None):
            opened.append(driver)

        def exec(self):
            return 0

    monkeypatch.setattr(main_window_module, "DeviceInfoDialog", RecordingDialog)
    window = MainWindow()
    try:
        assert window.device_label.text() == "Not connected"
        window.device_label.linkActivated.emit("device-info")
        assert opened == []  # nothing connected

        window.driver = EmulatedAnalyzerDriver(1)
        window._set_device_label("LOGIC_ANALYZER_PICO_<V6_5>")
        assert "href='device-info'" in window.device_label.text()
        assert "&lt;V6_5&gt;" in window.device_label.text()

        window.device_label.linkActivated.emit("device-info")
        assert opened == [window.driver]
    finally:
        window.driver = None
        window.close()
