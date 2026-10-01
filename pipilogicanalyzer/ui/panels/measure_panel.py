# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Measurements: the cursors A and B, and statistics of the channels over a range."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.formatting import to_large_frequency, to_small_time, to_thousands
from ...driver.models import EdgeKind
from ..icons import set_icon
from ..theme import set_role
from ..view_model import CaptureViewModel

#: Ranges the statistics cover
RANGE_CURSORS = "cursors"
RANGE_VIEW = "view"
RANGE_ALL = "all"
#: Columns of the statistics table
STATISTICS_COLUMNS = ("Channel", "Frequency", "Duty", "High min / max", "Low min / max", "Edges")


def _time(seconds: Optional[float]) -> str:
    return "–" if seconds is None else to_small_time(seconds)


def _card(parent: QWidget, title: str) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame(parent)
    set_role(card, "card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(10, 8, 10, 10)
    layout.setSpacing(6)
    heading = QLabel(title, card)
    set_role(heading, "heading")
    layout.addWidget(heading)
    return card, layout


def _table(parent: QWidget, columns: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns), parent)
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return table


class MeasurePanel(QWidget):
    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ------------------------------------------------------------ cursors
        card, cursor_layout = _card(self, "Cursors")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)
        self.cursor_values: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(
            (("A", "A"), ("B", "B"), ("delta", "Δt (B − A)"), ("frequency", "1 / Δt"), ("samples", "Δ samples"))
        ):
            name = QLabel(title, card)
            set_role(name, "hint")
            grid.addWidget(name, row, 0)
            value = QLabel("–", card)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(value, row, 1)
            self.cursor_values[key] = value
        cursor_layout.addLayout(grid)

        buttons = QHBoxLayout()
        self.place_button = QPushButton("Place in view", card)
        self.place_button.setToolTip("Put cursor A at a third and B at two thirds of the view")
        self.place_button.clicked.connect(self.place_cursors)
        buttons.addWidget(self.place_button)
        self.zoom_button = QPushButton("Zoom to A–B", card)
        set_icon(self.zoom_button, "fit")
        self.zoom_button.clicked.connect(self.zoom_to_cursors)
        buttons.addWidget(self.zoom_button)
        self.clear_button = QPushButton("Clear", card)
        self.clear_button.clicked.connect(model.clear_cursors)
        buttons.addWidget(self.clear_button)
        cursor_layout.addLayout(buttons)

        self.levels_table = _table(card, ("Channel", "at A", "at B", "Edges A–B"))
        self.levels_table.setMinimumHeight(120)
        cursor_layout.addWidget(self.levels_table, 1)
        hint = QLabel("Drag the cursors on the ruler; A, B and M place cursors and markers at the pointer.", card)
        hint.setWordWrap(True)
        set_role(hint, "hint")
        cursor_layout.addWidget(hint)
        layout.addWidget(card, 1)

        # --------------------------------------------------------- statistics
        card, statistics_layout = _card(self, "Statistics")
        row = QHBoxLayout()
        row.addWidget(QLabel("Range", card))
        self.range_combo = QComboBox(card)
        self.range_combo.addItem("Between the cursors", RANGE_CURSORS)
        self.range_combo.addItem("Visible part", RANGE_VIEW)
        self.range_combo.addItem("Whole capture", RANGE_ALL)
        self.range_combo.setCurrentIndex(2)
        self.range_combo.currentIndexChanged.connect(self._schedule)
        row.addWidget(self.range_combo, 1)
        statistics_layout.addLayout(row)
        self.statistics_table = _table(card, STATISTICS_COLUMNS)
        self.statistics_table.setMinimumHeight(140)
        statistics_layout.addWidget(self.statistics_table, 1)
        layout.addWidget(card, 1)

        # ---------------------------------------------------------- setup/hold
        card, timing_layout = _card(self, "Setup and hold")
        row = QHBoxLayout()
        self.data_combo = QComboBox(card)
        self.data_combo.setToolTip("Data channel")
        self.clock_combo = QComboBox(card)
        self.clock_combo.setToolTip("Clock channel")
        self.clock_edge_combo = QComboBox(card)
        self.clock_edge_combo.addItem("↑", EdgeKind.RISING)
        self.clock_edge_combo.addItem("↓", EdgeKind.FALLING)
        for label, widget in (("Data", self.data_combo), ("Clock", self.clock_combo)):
            row.addWidget(QLabel(label, card))
            row.addWidget(widget, 1)
        row.addWidget(self.clock_edge_combo)
        timing_layout.addLayout(row)
        self.timing_label = QLabel("–", card)
        self.timing_label.setWordWrap(True)
        self.timing_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        timing_layout.addWidget(self.timing_label)
        for combo in (self.data_combo, self.clock_combo, self.clock_edge_combo):
            combo.currentIndexChanged.connect(self._schedule)
        layout.addWidget(card)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.refresh_statistics)

        model.cursors_changed.connect(self._on_cursors_changed)
        model.capture_changed.connect(self._on_capture_changed)
        model.channels_changed.connect(self._on_capture_changed)
        model.view_changed.connect(self._on_view_changed)
        self._on_capture_changed()

    # ------------------------------------------------------------ cursors
    def place_cursors(self) -> None:
        first, visible = self.model.first_sample, self.model.visible_samples
        self.model.set_cursor("A", first + visible // 3)
        self.model.set_cursor("B", first + 2 * visible // 3)

    def zoom_to_cursors(self) -> None:
        a, b = self.model.cursor("A"), self.model.cursor("B")
        if a is None or b is None:
            return
        low, high = sorted((a, b))
        span = max(high - low, 4)
        self.model.set_view(low - span // 10, int(span * 1.2))

    def _on_cursors_changed(self) -> None:
        model = self.model
        a, b = model.cursor("A"), model.cursor("B")
        for name, sample in (("A", a), ("B", b)):
            self.cursor_values[name].setText(
                "–" if sample is None else f"{to_small_time(model.time_of(sample))}  ·  {to_thousands(sample)}"
            )
        delta = model.cursor_delta()
        seconds = None if delta is None else delta / max(model.frequency, 1)
        self.cursor_values["delta"].setText(_time(seconds))
        self.cursor_values["frequency"].setText(to_large_frequency(1 / abs(seconds)) if seconds else "–")
        self.cursor_values["samples"].setText("–" if delta is None else to_thousands(delta))
        self.zoom_button.setEnabled(delta is not None)
        self._fill_levels()
        if self.range_combo.currentData() == RANGE_CURSORS:
            self._schedule()

    def _fill_levels(self) -> None:
        model = self.model
        a, b = model.cursor("A"), model.cursor("B")
        channels = model.visible_channels
        self.levels_table.setRowCount(len(channels))
        for row, channel in enumerate(channels):
            transitions = model.transitions_for(channel)
            values = []
            for sample in (a, b):
                interval = transitions.interval_at(sample) if transitions is not None and sample is not None else None
                values.append("–" if interval is None else str(int(interval.value)))
            edges = "–"
            if transitions is not None and a is not None and b is not None:
                low, high = sorted((a, b))
                edges = to_thousands(max(transitions.run_index(high) - transitions.run_index(low), 0))
            for column, text in enumerate([channel.display_name, *values, edges]):
                self.levels_table.setItem(row, column, QTableWidgetItem(text))

    # --------------------------------------------------------- statistics
    def _on_capture_changed(self) -> None:
        channels = self.model.channels
        for combo in (self.data_combo, self.clock_combo):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for channel in channels:
                combo.addItem(channel.display_name, channel.channel_number)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else (1 if combo is self.data_combo and len(channels) > 1 else 0))
            combo.blockSignals(False)
        self._on_cursors_changed()
        self._schedule()

    def _on_view_changed(self) -> None:
        if self.range_combo.currentData() == RANGE_VIEW:
            self._schedule()

    def _schedule(self) -> None:
        self._timer.start()

    def statistics_range(self) -> Optional[tuple[int, int]]:
        model = self.model
        mode = self.range_combo.currentData()
        if mode == RANGE_CURSORS:
            a, b = model.cursor("A"), model.cursor("B")
            if a is None or b is None:
                return None
            low, high = sorted((a, b))
            return low, high + 1
        if mode == RANGE_VIEW:
            return model.first_sample, min(model.first_sample + model.visible_samples, model.sample_count)
        return 0, model.sample_count

    def refresh_statistics(self) -> None:
        from ...core.statistics import channel_statistics, setup_hold

        model = self.model
        session = model.session
        span = self.statistics_range()
        channels = model.visible_channels if session is not None else []
        self.statistics_table.setRowCount(len(channels) if span else 0)
        if not span or session is None or model.is_live:
            self.timing_label.setText("Place both cursors." if self.range_combo.currentData() == RANGE_CURSORS else "–")
            return
        start, end = span
        for row, channel in enumerate(channels):
            if channel.samples is None:
                continue
            stats = channel_statistics(channel.samples, session.frequency, start, end)
            frequency = to_large_frequency(stats.frequency) if stats.frequency else "–"
            duty = f"{stats.duty_cycle:.1f} %" if stats.duty_cycle is not None else "–"
            high = f"{_time(stats.high_min)} / {_time(stats.high_max)}"
            low = f"{_time(stats.low_min)} / {_time(stats.low_max)}"
            edges = to_thousands(stats.rising_edges + stats.falling_edges)
            for column, text in enumerate((channel.display_name, frequency, duty, high, low, edges)):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.statistics_table.setItem(row, column, item)

        by_number = {channel.channel_number: channel for channel in session.capture_channels}
        data = by_number.get(self.data_combo.currentData())
        clock = by_number.get(self.clock_combo.currentData())
        if data is None or clock is None or data is clock or data.samples is None or clock.samples is None:
            self.timing_label.setText("Choose a data and a different clock channel.")
            return
        result = setup_hold(data.samples, clock.samples, session.frequency, self.clock_edge_combo.currentData(), start, end)
        if result.setup_min is None:
            self.timing_label.setText("No clock edge with data changes around it in the range.")
            return
        self.timing_label.setText(
            f"Setup min {_time(result.setup_min)} (mean {_time(result.setup_mean)})  ·  "
            f"Hold min {_time(result.hold_min)} (mean {_time(result.hold_mean)})"
        )
