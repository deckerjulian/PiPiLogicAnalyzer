# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Board self-test: runs the firmware test and lists its results."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...driver.base import AnalyzerDriverBase, SelfTestResult, UnsupportedFeatureError
from ..icons import set_icon
from ..theme import set_role, set_variant
from .common import Banner, button_box, dialog_layout, heading, hint

SEVERITY_COLORS = {"ok": QColor(46, 122, 64), "warning": QColor(154, 98, 18), "fail": QColor(168, 50, 50)}
SEVERITY_TEXT = {"ok": "OK", "warning": "Warning", "fail": "Failed", "info": "Info"}

#: Explanations of the firmware status codes that are not self-explanatory.
STATUS_HINTS = {
    "STUCK_HIGH": "Reads high with the pull-down: signal connected, external buffer or short to 3.3 V",
    "STUCK_LOW": "Reads low with the pull-up: signal connected, external buffer or short to GND",
    "INVERTED": "Follows the pull resistors inverted",
    "ACTIVE": "Changes while no probe should be connected",
}


class SelfTestWorker(QThread):
    done = Signal(object)  # list[SelfTestResult] or Exception

    def __init__(self, driver: AnalyzerDriverBase, parent=None) -> None:
        super().__init__(parent)
        self._driver = driver

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            self.done.emit(self._driver.run_self_test())
        except Exception as error:  # noqa: BLE001 - shown in the dialog
            self.done.emit(error)


class BoardTestPanel(QWidget):
    """Runs the self-test of the device and lists its results (a tab of the device information)."""

    running_changed = Signal(bool)

    def __init__(self, driver: AnalyzerDriverBase, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.driver = driver
        self.results: list[SelfTestResult] = []
        self._worker: Optional[SelfTestWorker] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(hint(driver.self_test_description, self))
        banner = Banner("warning", self)
        banner.set_message("<b>Disconnect all probes and signals</b> before running the test.")
        layout.addWidget(banner)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Test", "Result", "Details"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.summary = hint("Press Run self-test to start.", self)
        row.addWidget(self.summary, 1)
        self.start_button = QPushButton("Run self-test", self)
        set_variant(self.start_button, "primary")
        set_icon(self.start_button, "play")
        self.start_button.clicked.connect(self.start)
        row.addWidget(self.start_button)
        layout.addLayout(row)

    @property
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def start(self) -> None:
        if self.is_running:
            return
        self.table.setRowCount(0)
        self.start_button.setEnabled(False)
        self._set_summary("Running the self-test, this takes a few seconds...", "hint")
        self._worker = SelfTestWorker(self.driver, self)
        self._worker.done.connect(self._on_done)
        self._worker.start()
        self.running_changed.emit(True)

    def _set_summary(self, text: str, role: str) -> None:
        self.summary.setText(text)
        set_role(self.summary, role)

    def _on_done(self, outcome: object) -> None:
        self.start_button.setEnabled(True)
        self.start_button.setText("Run again")
        self.running_changed.emit(False)
        if isinstance(outcome, UnsupportedFeatureError):
            self._set_summary(
                "The firmware of this device has no self-test. Flash the firmware from the "
                "firmware/ folder of this project (see firmware/README.md).",
                "warning",
            )
            return
        if isinstance(outcome, Exception):
            self._set_summary(f"The self-test could not be completed: {outcome}", "error")
            return
        self.show_results(list(outcome))

    def show_results(self, results: list[SelfTestResult]) -> None:
        self.results = results
        self.table.setRowCount(len(results))
        for row, result in enumerate(results):
            severity = result.severity
            detail = result.detail
            status_hint = STATUS_HINTS.get(result.status)
            if status_hint:
                detail = f"{status_hint}. {detail}".strip()

            cells = (result.title, SEVERITY_TEXT[severity], detail)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 1 and severity in SEVERITY_COLORS:
                    item.setBackground(SEVERITY_COLORS[severity])
                    item.setForeground(Qt.white)
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)

        failed = sum(result.severity == "fail" for result in results)
        warnings = sum(result.severity == "warning" for result in results)
        passed = sum(result.severity == "ok" for result in results)
        if failed:
            self._set_summary(f"{failed} test(s) failed, {warnings} warning(s), {passed} passed.", "error")
        elif warnings:
            self._set_summary(f"No failures, {warnings} warning(s), {passed} passed.", "warning")
        else:
            self._set_summary(f"All {passed} tests passed.", "success")


class BoardTestDialog(QDialog):
    """The self-test in a window of its own."""

    def __init__(self, driver: AnalyzerDriverBase, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Board self-test")
        self.resize(700, 580)
        layout = dialog_layout(self)
        layout.addWidget(heading(driver.device_version or "Device", self))
        self.panel = BoardTestPanel(driver, self)
        layout.addWidget(self.panel, 1)
        buttons = button_box(self, None)
        layout.addWidget(buttons)
        self.panel.running_changed.connect(
            lambda running: buttons.button(QDialogButtonBox.Close).setEnabled(not running)
        )
        self.table = self.panel.table
        self.summary = self.panel.summary
        self.start_button = self.panel.start_button

    @property
    def results(self) -> list[SelfTestResult]:
        return self.panel.results

    def start(self) -> None:
        self.panel.start()

    def _on_done(self, outcome: object) -> None:
        self.panel._on_done(outcome)

    def show_results(self, results: list[SelfTestResult]) -> None:
        self.panel.show_results(results)

    def done(self, result: int) -> None:  # noqa: D401 - QDialog override
        # Esc or the window close button: the worker still talks to the device.
        if self.panel.is_running:
            return
        super().done(result)
