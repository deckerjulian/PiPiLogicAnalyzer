# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compares the loaded capture with a reference capture and marks where they differ."""

from __future__ import annotations

import os
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...core import capture_io
from ...core.formatting import to_small_time, to_thousands
from ...driver.models import CaptureSession
from ..icons import set_icon
from ..view_model import CaptureViewModel
from .common import InlineMessage, button_box, dialog_layout, hint

REFERENCE_FILE_FILTER = "Captures (*.lac *.lac.gz *.sr);;All files (*)"


def load_reference(path: str) -> CaptureSession:
    if path.lower().endswith(".sr"):
        from ...core.sigrok_session import load_session

        return load_session(path)
    return capture_io.load_capture(path).session


class CompareDialog(QDialog):
    """*Compare* marks the differences as search matches; *Mark as regions* keeps them as regions."""

    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self.reference: Optional[CaptureSession] = None
        self.result = None
        self.setWindowTitle("Compare with a reference")
        self.resize(640, 520)
        layout = dialog_layout(self)
        layout.addWidget(
            hint(
                "Compares every channel with the channel of the same number in the reference capture. "
                "Differences are shown as search matches (F3 jumps to the next one).",
                self,
            )
        )

        form = QFormLayout()
        row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("Reference capture file")
        row.addWidget(self.path_edit, 1)
        browse = QPushButton("Choose...", self)
        set_icon(browse, "folder")
        browse.clicked.connect(self._choose)
        row.addWidget(browse)
        form.addRow("Reference", row)

        row = QHBoxLayout()
        self.offset_box = QSpinBox(self)
        self.offset_box.setRange(-2_000_000_000, 2_000_000_000)
        self.offset_box.setToolTip("Capture sample i is compared with reference sample i + offset")
        row.addWidget(self.offset_box, 1)
        self.auto_box = QCheckBox("Find automatically", self)
        self.auto_box.setChecked(True)
        row.addWidget(self.auto_box)
        form.addRow("Offset (samples)", row)

        self.tolerance_box = QSpinBox(self)
        self.tolerance_box.setRange(0, 1_000_000)
        self.tolerance_box.setValue(1)
        self.tolerance_box.setToolTip("Differences of at most this many samples are ignored (edge jitter)")
        form.addRow("Tolerance (samples)", self.tolerance_box)
        layout.addLayout(form)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Channel", "Differences", "First at"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        buttons = button_box(self, "Compare", cancel_text="Close", icon="compare")
        self.regions_button = buttons.addButton("Mark as regions", QDialogButtonBox.ActionRole)
        self.regions_button.setEnabled(False)
        self.regions_button.clicked.connect(self._mark_regions)
        buttons.accepted.connect(self.compare)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Reference capture", "", REFERENCE_FILE_FILTER)
        if path:
            self.set_reference_file(path)

    def set_reference_file(self, path: str) -> None:
        try:
            self.reference = load_reference(path)
        except (OSError, ValueError, KeyError) as error:
            self.message.show_error(f"{os.path.basename(path)} could not be read: {error}")
            return
        self.path_edit.setText(path)
        self.message.clear()

    def compare(self) -> None:
        from ...core.compare import align_offset, compare_sessions

        session = self.model.session
        if session is None or self.reference is None:
            self.message.show_error("Choose a reference capture first.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            offset = self.offset_box.value()
            if self.auto_box.isChecked():
                offset = align_offset(self.reference, session, max(self.model.sample_count // 4, 16))
                self.offset_box.setValue(offset)
            result = compare_sessions(self.reference, session, offset, self.tolerance_box.value())
        finally:
            QApplication.restoreOverrideCursor()
        self.result = result
        self._show(result)

    def _show(self, result) -> None:
        names = {channel.channel_number: channel.display_name for channel in self.model.channels}
        rows = sorted(result.differences.items())
        self.table.setRowCount(len(rows))
        for row, (number, ranges) in enumerate(rows):
            first = to_small_time(self.model.time_of(ranges[0][0])) if ranges else "–"
            for column, text in enumerate((names.get(number, f"CH{number + 1}"), to_thousands(len(ranges)), first)):
                self.table.setItem(row, column, QTableWidgetItem(text))
        if result.identical:
            self.message.show_success(
                f"Identical over {to_thousands(result.compared_samples)} samples (offset {result.offset})."
            )
        else:
            self.message.show_error(
                f"{to_thousands(result.difference_count)} differences, {to_thousands(result.differing_samples)} "
                f"differing samples (offset {result.offset})."
            )
        ranges = sorted(item for spans in result.differences.values() for item in spans)
        starts = np.array([start for start, _end in ranges], dtype=np.int64)
        ends = np.array([end for _start, end in ranges], dtype=np.int64)
        self.model.set_search_hits(starts, ends)
        if len(starts):
            self.model.select_search_hit(0)
        self.regions_button.setEnabled(not result.identical)

    def _mark_regions(self) -> None:
        from ...core.compare import difference_regions

        if self.result is not None:
            self.model.add_regions(difference_regions(self.result))
            self.message.show_success("The differences were added as regions.")
