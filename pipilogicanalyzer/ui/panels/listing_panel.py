# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The capture as a table: one row per change of the channels and buses, or per decoder annotation.

The rows are computed from the edge index of the view model; the table model only formats the
rows Qt asks for, so captures with millions of changes stay responsive.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...core import colors
from ...core.formatting import to_small_time, to_thousands
from ..theme import set_role
from ..view_model import CaptureViewModel

MODE_CHANGES = "changes"
MODE_DECODER = "decoder"
#: Rows of the change listing at most (the first ones of the capture)
MAX_ROWS = 20_000_000


class ListingModel(QAbstractTableModel):
    def __init__(self, model: CaptureViewModel, parent=None) -> None:
        super().__init__(parent)
        self.view_model = model
        self.mode = MODE_CHANGES
        self.positions = np.empty(0, dtype=np.int64)
        #: Decoder rows: (first sample, last sample, decoder, row, text)
        self.annotations: list[tuple[int, int, str, str, str]] = []
        self.columns: list[str] = []
        self._channels: list = []
        self._buses: list = []

    # --------------------------------------------------------------- content
    def rebuild(self, mode: str) -> None:
        self.beginResetModel()
        self.mode = mode
        model = self.view_model
        if mode == MODE_DECODER:
            rows = []
            for group in model.annotation_groups:
                for annotation in getattr(group, "annotations", []):
                    for segment in annotation.segments:
                        text = segment.values[0] if segment.values else ""
                        rows.append((segment.first_sample, segment.last_sample, group.decoder_name, annotation.name, text))
            rows.sort(key=lambda row: row[0])
            self.annotations = rows
            self.positions = np.array([row[0] for row in rows], dtype=np.int64)
            self.columns = ["#", "Time", "Duration", "Decoder", "Row", "Value"]
        else:
            self._channels = model.visible_channels
            self._buses = model.buses
            starts = [np.zeros(1, dtype=np.int64)]
            for channel in self._channels:
                transitions = model.transitions_for(channel)
                if transitions is not None and len(transitions):
                    starts.append(np.asarray(transitions.starts, dtype=np.int64))
            positions = np.unique(np.concatenate(starts)) if model.sample_count else np.empty(0, dtype=np.int64)
            self.positions = positions[:MAX_ROWS]
            self.columns = ["#", "Time", "Sample"] + [bus.name for bus in self._buses] + [
                channel.display_name for channel in self._channels
            ]
        self.endResetModel()

    def row_of_sample(self, sample: int) -> int:
        return max(int(np.searchsorted(self.positions, sample, side="right")) - 1, 0)

    # ------------------------------------------------------------- Qt model
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else len(self.positions)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole) -> Any:  # noqa: N802 - Qt naming
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and section < len(self.columns):
            return self.columns[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row, column = index.row(), index.column()
        sample = int(self.positions[row])
        model = self.view_model
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter) if column < 3 else int(Qt.AlignLeft | Qt.AlignVCenter)
        if self.mode == MODE_DECODER:
            first, last, decoder, row_name, text = self.annotations[row]
            if role != Qt.DisplayRole:
                return None
            return (
                str(row + 1),
                to_small_time(model.time_of(first)),
                to_small_time((last - first) / max(model.frequency, 1)),
                decoder,
                row_name,
                text,
            )[column]

        bus_count = len(self._buses)
        if column >= 3 + bus_count:
            channel = self._channels[column - 3 - bus_count]
            level = int(channel.samples[sample]) if channel.samples is not None and sample < len(channel.samples) else 0
            if role == Qt.DisplayRole:
                return str(level)
            if role == Qt.ForegroundRole:
                return colors.get_channel_color(channel) if level else QColor(120, 120, 128)
            return None
        if role != Qt.DisplayRole:
            return None
        if column == 0:
            return str(row + 1)
        if column == 1:
            return to_small_time(model.time_of(sample))
        if column == 2:
            return to_thousands(sample)
        from ...core.buses import format_value

        bus = self._buses[column - 3]
        values = model.bus_values(bus)
        return format_value(int(values[sample]), bus) if sample < len(values) else ""


class ListingPanel(QWidget):
    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)

        row = QHBoxLayout()
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("Changes of channels and buses", MODE_CHANGES)
        self.mode_combo.addItem("Decoder output", MODE_DECODER)
        self.mode_combo.currentIndexChanged.connect(self.rebuild)
        row.addWidget(self.mode_combo)
        self.follow_box = QCheckBox("Follow the view", self)
        self.follow_box.setChecked(True)
        self.follow_box.setToolTip("Scroll the listing to the first row in view")
        row.addWidget(self.follow_box)
        row.addStretch(1)
        self.count_label = QLabel(self)
        set_role(self.count_label, "hint")
        row.addWidget(self.count_label)
        layout.addLayout(row)

        self.table_model = ListingModel(model, self)
        self.table = QTableView(self)
        self.table.setModel(self.table_model)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultSectionSize(90)
        self.table.clicked.connect(self._row_clicked)
        layout.addWidget(self.table, 1)

        self._dirty = True
        model.capture_changed.connect(self._invalidate)
        model.channels_changed.connect(self._invalidate)
        model.buses_changed.connect(self._invalidate)
        model.annotations_changed.connect(self._on_annotations_changed)
        model.view_changed.connect(self._follow)

    def _invalidate(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.rebuild()

    def _on_annotations_changed(self) -> None:
        if self.mode_combo.currentData() == MODE_DECODER:
            self._invalidate()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if self._dirty:
            self.rebuild()

    def rebuild(self) -> None:
        self._dirty = False
        if self.model.is_live:
            return
        self.table_model.rebuild(self.mode_combo.currentData())
        count = self.table_model.rowCount()
        self.count_label.setText(f"{to_thousands(count)} rows")
        self._follow()

    def _follow(self) -> None:
        if not self.follow_box.isChecked() or not self.isVisible() or not self.table_model.rowCount():
            return
        row = self.table_model.row_of_sample(self.model.first_sample)
        self.table.scrollTo(self.table_model.index(row, 0), QAbstractItemView.PositionAtTop)

    def _row_clicked(self, index: QModelIndex) -> None:
        sample = int(self.table_model.positions[index.row()])
        following = self.follow_box.isChecked()
        self.follow_box.setChecked(False)
        if not self.model.first_sample <= sample <= self.model.last_sample:
            self.model.center_on(sample)
        self.model.set_user_marker(sample)
        self.follow_box.setChecked(following)
