# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Search the capture: edges, patterns, pulse widths, gaps, bus values and decoder output."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.formatting import parse_time, to_small_time, to_thousands
from ...driver.models import ConditionKind, EdgeKind, TriggerCondition
from ..icons import set_icon
from .. import background
from ..theme import set_role, set_variant
from ..view_model import CaptureViewModel

#: Matches listed in the table (all of them are marked in the waveform)
MAX_LISTED = 5000

KIND_EDGE = "edge"
KIND_PATTERN = "pattern"
KIND_PULSE = "pulse"
KIND_GAP = "gap"
KIND_BUS = "bus"
KIND_DECODER = "decoder"
KINDS = (
    (KIND_EDGE, "Edge"),
    (KIND_PATTERN, "Pattern"),
    (KIND_PULSE, "Pulse width"),
    (KIND_GAP, "Gap (no edge for)"),
    (KIND_BUS, "Bus value"),
    (KIND_DECODER, "Decoder output"),
)
BUS_OPERATORS = ("==", "!=", "<", "<=", ">", ">=", "in")


def parse_pattern(text: str, first_channel: int) -> tuple[int, int]:
    """``(mask, value)`` of a pattern such as ``10X1``: the first character is ``first_channel``."""
    mask = value = 0
    bits = text.replace(" ", "").upper()
    if not bits or any(bit not in "01X" for bit in bits):
        raise ValueError("A pattern consists of 0, 1 and X (any level), e.g. 10X1")
    for offset, bit in enumerate(bits):
        if bit == "X":
            continue
        mask |= 1 << (first_channel + offset)
        if bit == "1":
            value |= 1 << (first_channel + offset)
    if not mask:
        raise ValueError("The pattern has to set at least one channel to 0 or 1")
    return mask, value


class SearchPanel(QWidget):
    def __init__(self, model: CaptureViewModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        #: Text of each listed decoder match
        self._labels: list[str] = []
        #: A search ran on the loaded capture
        self._searched = False
        self._searching = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.kind_combo = QComboBox(self)
        for key, label in KINDS:
            self.kind_combo.addItem(label, key)
        layout.addWidget(self.kind_combo)

        self.pages = QStackedWidget(self)
        self.pages.addWidget(self._edge_page())
        self.pages.addWidget(self._pattern_page())
        self.pages.addWidget(self._pulse_page())
        self.pages.addWidget(self._gap_page())
        self.pages.addWidget(self._bus_page())
        self.pages.addWidget(self._decoder_page())
        self.kind_combo.currentIndexChanged.connect(self.pages.setCurrentIndex)
        layout.addWidget(self.pages)

        row = QHBoxLayout()
        self.find_button = QPushButton("Find all", self)
        set_variant(self.find_button, "primary")
        set_icon(self.find_button, "search")
        self.find_button.clicked.connect(self.search)
        row.addWidget(self.find_button)
        self.previous_button = QPushButton(self)
        set_icon(self.previous_button, "up")
        self.previous_button.setToolTip("Previous match (Shift+F3)")
        self.previous_button.clicked.connect(lambda: self.step(-1))
        row.addWidget(self.previous_button)
        self.next_button = QPushButton(self)
        set_icon(self.next_button, "down")
        self.next_button.setToolTip("Next match (F3)")
        self.next_button.clicked.connect(lambda: self.step(1))
        row.addWidget(self.next_button)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.clicked.connect(self.clear)
        row.addWidget(self.clear_button)
        layout.addLayout(row)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        set_role(self.status, "hint")
        layout.addWidget(self.status)

        self.results = QTableWidget(0, 3, self)
        self.results.setHorizontalHeaderLabels(["#", "Time", "Match"])
        self.results.verticalHeader().setVisible(False)
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setShowGrid(False)
        self.results.setAlternatingRowColors(True)
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.results.currentCellChanged.connect(lambda row, *_: self._select(row))
        layout.addWidget(self.results, 1)

        model.capture_changed.connect(self._on_capture_changed)
        model.buses_changed.connect(self._fill_buses)
        model.channels_changed.connect(self._fill_channels)
        model.search_changed.connect(self._sync_selection)
        self._on_capture_changed()

    # -------------------------------------------------------------- pages
    def _form(self) -> tuple[QWidget, QFormLayout]:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        return page, form

    def _channel_combo(self, page: QWidget) -> QComboBox:
        combo = QComboBox(page)
        self._channel_combos.append(combo)
        return combo

    def _edge_page(self) -> QWidget:
        self._channel_combos: list[QComboBox] = []
        page, form = self._form()
        self.edge_channel = self._channel_combo(page)
        form.addRow("Channel", self.edge_channel)
        self.edge_kind = QComboBox(page)
        for label, kind in (("Rising ↑", EdgeKind.RISING), ("Falling ↓", EdgeKind.FALLING), ("Any ↕", EdgeKind.ANY)):
            self.edge_kind.addItem(label, kind)
        form.addRow("Edge", self.edge_kind)
        return page

    def _pattern_page(self) -> QWidget:
        page, form = self._form()
        self.pattern_channel = self._channel_combo(page)
        form.addRow("First channel", self.pattern_channel)
        self.pattern_edit = QLineEdit(page)
        self.pattern_edit.setPlaceholderText("e.g. 10X1 (X: any level)")
        form.addRow("Pattern", self.pattern_edit)
        return page

    def _pulse_page(self) -> QWidget:
        page, form = self._form()
        self.pulse_channel = self._channel_combo(page)
        form.addRow("Channel", self.pulse_channel)
        self.pulse_level = QComboBox(page)
        for label, kind in (("High", EdgeKind.RISING), ("Low", EdgeKind.FALLING), ("High or low", EdgeKind.ANY)):
            self.pulse_level.addItem(label, kind)
        form.addRow("Level", self.pulse_level)
        self.pulse_min = QLineEdit(page)
        self.pulse_min.setPlaceholderText("e.g. 1 µs (empty: no minimum)")
        form.addRow("At least", self.pulse_min)
        self.pulse_max = QLineEdit(page)
        self.pulse_max.setPlaceholderText("e.g. 2 µs (empty: no maximum)")
        form.addRow("At most", self.pulse_max)
        return page

    def _gap_page(self) -> QWidget:
        page, form = self._form()
        self.gap_channel = self._channel_combo(page)
        form.addRow("Channel", self.gap_channel)
        self.gap_time = QLineEdit(page)
        self.gap_time.setPlaceholderText("e.g. 10 ms")
        form.addRow("No edge for", self.gap_time)
        return page

    def _bus_page(self) -> QWidget:
        page, form = self._form()
        self.bus_combo = QComboBox(page)
        form.addRow("Bus", self.bus_combo)
        row = QHBoxLayout()
        self.bus_operator = QComboBox(page)
        self.bus_operator.addItems(BUS_OPERATORS)
        row.addWidget(self.bus_operator)
        self.bus_value = QLineEdit(page)
        self.bus_value.setPlaceholderText("0x1F, 31, 0b11111 or a symbol")
        row.addWidget(self.bus_value, 1)
        form.addRow("Value", row)
        self.bus_value2 = QLineEdit(page)
        self.bus_value2.setPlaceholderText("upper end for 'in'")
        form.addRow("To", self.bus_value2)
        return page

    def _decoder_page(self) -> QWidget:
        page, form = self._form()
        self.decoder_text = QLineEdit(page)
        self.decoder_text.setPlaceholderText("Text of an annotation, e.g. ACK or 0x4F")
        self.decoder_text.returnPressed.connect(self.search)
        form.addRow("Text", self.decoder_text)
        self.decoder_filter = QComboBox(page)
        form.addRow("Decoder", self.decoder_filter)
        options = QHBoxLayout()
        self.regex_box = QCheckBox("Regular expression", page)
        options.addWidget(self.regex_box)
        self.case_box = QCheckBox("Match case", page)
        options.addWidget(self.case_box)
        form.addRow("", options)
        self.model.annotations_changed.connect(self._fill_decoders)
        return page

    # -------------------------------------------------------------- lists
    def _on_capture_changed(self) -> None:
        self._searched = False
        self._labels = []
        if self.model.search_hits.starts.size:
            # the samples changed: the positions found before no longer mark the matches
            self.model.set_search_hits(np.empty(0, dtype=np.int64))
        self._fill_channels()
        self._fill_buses()
        self._fill_decoders()
        self._show_results()

    def _fill_channels(self) -> None:
        channels = self.model.channels
        for combo in self._channel_combos:
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for channel in channels:
                combo.addItem(channel.display_name, channel.channel_number)
            combo.setCurrentIndex(max(combo.findData(current), 0))
            combo.blockSignals(False)
        enabled = bool(channels)
        for button in (self.find_button, self.previous_button, self.next_button):
            button.setEnabled(enabled)

    def _fill_buses(self) -> None:
        self.bus_combo.clear()
        for bus in self.model.buses:
            self.bus_combo.addItem(bus.name, bus)

    def _fill_decoders(self) -> None:
        current = self.decoder_filter.currentData()
        self.decoder_filter.clear()
        self.decoder_filter.addItem("Every decoder", None)
        for name in sorted({group.decoder_name for group in self.model.annotation_groups}):
            self.decoder_filter.addItem(name, name)
        self.decoder_filter.setCurrentIndex(max(self.decoder_filter.findData(current), 0))

    # ------------------------------------------------------------- search
    def _condition(self, kind: str) -> TriggerCondition:
        if kind == KIND_EDGE:
            return TriggerCondition(ConditionKind.EDGE, self.edge_channel.currentData(), self.edge_kind.currentData())
        if kind == KIND_PATTERN:
            mask, value = parse_pattern(self.pattern_edit.text(), self.pattern_channel.currentData())
            return TriggerCondition(ConditionKind.PATTERN, mask=mask, value=value)
        if kind == KIND_PULSE:
            minimum = self.pulse_min.text().strip()
            maximum = self.pulse_max.text().strip()
            return TriggerCondition(
                ConditionKind.PULSE,
                self.pulse_channel.currentData(),
                self.pulse_level.currentData(),
                min_ns=round(parse_time(minimum) * 1e9) if minimum else None,
                max_ns=round(parse_time(maximum) * 1e9) if maximum else None,
            )
        return TriggerCondition(
            ConditionKind.GAP, self.gap_channel.currentData(), min_ns=round(parse_time(self.gap_time.text()) * 1e9)
        )

    def search(self) -> None:
        session = self.model.session
        if session is None or not self.model.sample_count:
            return
        kind = self.kind_combo.currentData()
        if self._searching:
            return
        self._searching = True
        try:
            # What is searched for is read from the fields here; the search itself runs in the
            # background (seconds for a large capture), with a dialog to cancel it.
            find = self._search_job(kind, session)
            starts, ends, labels = background.run(self, "Searching...", find)
        except background.Cancelled:
            return
        except ValueError as error:
            self.status.setText(str(error))
            set_role(self.status, "error")
            return
        except Exception as error:  # noqa: BLE001 - e.g. a broken regular expression
            self.status.setText(f"The search failed: {error}")
            set_role(self.status, "error")
            return
        finally:
            self._searching = False
        if self.model.session is not session:
            return  # other data meanwhile: the positions are not its matches
        self._labels = labels
        set_role(self.status, "hint")
        self._searched = True
        self.model.set_search_hits(starts, ends)
        self._show_results()
        if len(starts):
            self.step(1)

    def _search_job(self, kind, session):
        """A function that finds the matches: ``(starts, ends or None, labels)``."""
        if kind == KIND_DECODER:
            from ...core.search import find_annotations

            groups = self.model.annotation_groups
            text, regex, case = self.decoder_text.text(), self.regex_box.isChecked(), self.case_box.isChecked()
            decoder = self.decoder_filter.currentData()

            def find_text():
                matches = find_annotations(groups, text, regex=regex, case_sensitive=case, decoder=decoder)
                return (np.array([match.first_sample for match in matches], dtype=np.int64),
                        np.array([match.last_sample for match in matches], dtype=np.int64),
                        [f"{match.decoder}: {match.text}" for match in matches[:MAX_LISTED]])

            return find_text
        if kind == KIND_BUS:
            from ...core.buses import parse_value
            from ...core.search import find_bus_values

            bus = self.bus_combo.currentData()
            if bus is None:
                raise ValueError("Define a bus first (Analyze > New bus or group...).")
            operand = parse_value(self.bus_value.text(), bus)
            operator = self.bus_operator.currentText()
            operand2 = parse_value(self.bus_value2.text(), bus) if operator == "in" else None
            values = self.model.bus_values(bus)  # (kept by the model: not computed in the thread)
            return lambda: (find_bus_values(values, operator, operand, operand2), None, [])
        from ...core.conditions import condition_events

        channels = {channel.channel_number: channel.samples for channel in session.capture_channels
                    if channel.samples is not None}
        condition, frequency = self._condition(kind), session.frequency
        return lambda: (condition_events(channels, condition, frequency), None, [])

    def _show_results(self) -> None:
        hits = self.model.search_hits
        count = len(hits)
        self.results.blockSignals(True)
        self.results.setRowCount(min(count, MAX_LISTED))
        for row in range(min(count, MAX_LISTED)):
            start = int(hits.starts[row])
            label = self._labels[row] if row < len(self._labels) else f"sample {to_thousands(start)}"
            for column, text in enumerate((str(row + 1), to_small_time(self.model.time_of(start)), label)):
                self.results.setItem(row, column, QTableWidgetItem(text))
        self.results.blockSignals(False)
        if not count:
            self.status.setText("No match." if self._searched else "")
        else:
            listed = f", the first {to_thousands(MAX_LISTED)} listed" if count > MAX_LISTED else ""
            self.status.setText(f"{to_thousands(count)} matches{listed}")

    def step(self, direction: int) -> None:
        """Selects the next (1) or previous (-1) match after the view's centre."""
        hits = self.model.search_hits
        if not len(hits):
            return
        if hits.current < 0:
            center = self.model.first_sample + self.model.visible_samples // 2
            index = int(np.searchsorted(hits.starts, center))
            index = index if direction > 0 else index - 1
        else:
            index = hits.current + direction
        self.model.select_search_hit(index)

    def clear(self) -> None:
        self._labels = []
        self._searched = False
        self.model.set_search_hits(np.empty(0, dtype=np.int64))
        self._show_results()
        self.status.setText("")

    def _select(self, row: int) -> None:
        if row >= 0 and row != self.model.search_hits.current:
            self.model.select_search_hit(row)

    def _sync_selection(self) -> None:
        current = self.model.search_hits.current
        if 0 <= current < self.results.rowCount() and self.results.currentRow() != current:
            self.results.blockSignals(True)
            self.results.selectRow(current)
            self.results.blockSignals(False)
