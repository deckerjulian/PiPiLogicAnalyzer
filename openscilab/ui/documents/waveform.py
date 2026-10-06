# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The waveform document: build a waveform or a pattern, see it, play it on a generator output.

A form per kind (standard shapes with sweep and burst, arbitrary points from a formula or a CSV
file, patterns as SDL per pin with a preview of the tracks), the outputs of every instrument of
the hub with a generator, and Start/Stop. Waveforms beyond the limits of the chosen output are
explained before they are played. Files: ``*.wave.yaml`` (and ``*.sdl`` to open).
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import units
from ...core import waveform as waves
from ...core.hub import Hub
from ...core.instrument import GeneratorFacet, InstrumentError
from .. import messages
from ..devices.hub_bridge import HubBridge
from ..icons import set_icon
from ..theme import BORDER, TEXT_MUTED, set_role, set_variant, token
from ..widgets.plot_lines import screen_points
from .base import DocumentWidget

WAVE_FILE_FILTER = "Waveforms (*.wave.yaml);;SDL patterns (*.sdl);;All files (*)"
WAVE_EXTENSIONS = (".wave.yaml", ".sdl")
MODULATIONS = (("None", ""), ("Sweep", waves.SWEEP), ("Burst", waves.BURST))


def tracks_text(waveform: waves.Waveform) -> str:
    """The tracks of a pattern as ``pin: SDL`` lines."""
    return "\n".join(f"{pin}: {waveform.sdl.get(pin) or waves.levels_to_sdl(levels)}"
                     for pin, levels in waveform.tracks.items())


def parse_tracks(text: str) -> dict[str, str]:
    """``pin: SDL`` lines (``//`` comments allowed) as SDL per pin."""
    tracks: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("//", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise waves.WaveformError(f"line {number}: 'pin: SDL', e.g. 'D0: l5;h5;'")
        pin, sdl = line.split(":", 1)
        tracks[pin.strip()] = sdl.strip()
    return tracks


class WavePreview(QWidget):
    """Two passes of a waveform, or the tracks of a pattern."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.waveform: Optional[waves.Waveform] = None
        #: arbitrary points from a file or a capture (used while the formula is empty)
        self._points: Optional[waves.Waveform] = None
        self.error = ""
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_waveform(self, waveform: Optional[waves.Waveform], error: str = "") -> None:
        self.waveform, self.error = waveform, error
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(self.rect()).adjusted(46, 12, -12, -22)
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawRect(area)
        wave = self.waveform
        if wave is None:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(area, Qt.AlignCenter, self.error or "No waveform")
            return
        if wave.is_pattern:
            self._draw_pattern(painter, area, wave)
        else:
            self._draw_analog(painter, area, wave)

    def _draw_analog(self, painter: QPainter, area: QRectF, wave: waves.Waveform) -> None:
        span = 2 * wave.period if np.isfinite(wave.period) and wave.period > 0 else 1e-3
        if wave.modulation == waves.SWEEP:
            span = wave.sweep_time
        elif wave.modulation == waves.BURST:
            span = 2 * wave.burst_period
        count = max(int(area.width()) * 4, 16)
        times = np.arange(count) * span / count
        volts = wave.analog(times)
        low, high = float(volts.min()), float(volts.max())
        if high - low < 1e-12:
            low, high = low - 1, high + 1
        margin = (high - low) * 0.1
        low, high = low - margin, high + margin
        painter.setPen(QPen(QColor(token("type.analog")), 1.5))
        # (many more points than pixels are drawn as their envelope)
        painter.drawPolyline(screen_points(np.arange(count), volts, (0.0, float(count), low, high), area))
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(0, area.top() - 6, 44, 14), Qt.AlignRight, units.format_quantity(high - margin, "V", 3))
        painter.drawText(QRectF(0, area.bottom() - 8, 44, 14), Qt.AlignRight, units.format_quantity(low + margin, "V", 3))
        painter.drawText(QRectF(area.left(), area.bottom() + 4, area.width(), 16), Qt.AlignRight,
                         units.format_quantity(span, "s", 3))

    def _draw_pattern(self, painter: QPainter, area: QRectF, wave: waves.Waveform) -> None:
        tracks = list(wave.tracks.items())
        if not tracks:
            return
        length = max(wave.pattern_length, 1)
        row = area.height() / len(tracks)
        painter.setPen(QPen(QColor(token("type.digital")), 1.5))
        for index, (pin, levels) in enumerate(tracks):
            top = area.top() + index * row + row * 0.2
            bottom = area.top() + (index + 1) * row - row * 0.2
            points = pattern_points(np.asarray(levels), length, area, top, bottom)
            if points:
                painter.drawPolyline(points)
            painter.save()
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(0, top, 42, bottom - top), Qt.AlignRight | Qt.AlignVCenter, pin)
            painter.restore()
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(QRectF(area.left(), area.bottom() + 4, area.width(), 16), Qt.AlignRight,
                         f"{length} samples, {units.format_quantity(wave.period, 's', 3)}")


def pattern_points(levels: np.ndarray, length: int, area: QRectF, top: float, bottom: float) -> list[QPointF]:
    """The line of a digital track of ``length`` samples across ``area`` (high at ``top``).

    Drawn from its runs, not from every sample: a capture played as a signal has millions of
    samples. With more runs than pixels a column that holds both levels becomes a bar.
    """
    count = len(levels)
    if not count:
        return []
    high = np.asarray(levels) != 0
    scale = area.width() / max(length, 1)
    changes = np.flatnonzero(high[1:] != high[:-1]) + 1
    columns = max(int(area.width()), 1)
    if len(changes) <= 4 * columns:
        starts = np.concatenate(([0], changes))
        ends = np.concatenate((changes, [count]))
        points = []
        for start, end, level in zip(starts.tolist(), ends.tolist(), high[starts].tolist()):
            y = top if level else bottom
            points.append(QPointF(area.left() + start * scale, y))
            points.append(QPointF(area.left() + end * scale, y))
        return points
    edges = np.unique((np.arange(columns + 1) * count / columns).astype(np.int64))
    starts = edges[:-1][edges[:-1] < count]
    levels8 = high.view(np.uint8)
    low_level, high_level = np.minimum.reduceat(levels8, starts), np.maximum.reduceat(levels8, starts)
    points = []
    for start, low, up in zip(starts.tolist(), low_level.tolist(), high_level.tolist()):
        x = area.left() + start * scale
        if low != up:  # both levels in this column
            points.append(QPointF(x, bottom))
            points.append(QPointF(x, top))
        else:
            points.append(QPointF(x, top if up else bottom))
    return points


class WaveformDocument(DocumentWidget):
    """Build, preview and play a waveform (see the module documentation)."""

    document_kind = "waveform"
    #: (instrument name, output) started or stopped
    output_changed = Signal()

    def __init__(self, hub: Hub, waveform: Optional[waves.Waveform] = None, path: Optional[str] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.hub = hub
        self.bridge = HubBridge(hub, self)
        self.bridge.changed.connect(lambda _event: self.refresh_outputs())
        self._path = path
        self._dirty = False
        self._filling = False
        self.waveform: Optional[waves.Waveform] = None
        self.error = ""
        #: (instrument, output) playing from this document
        self.playing: Optional[tuple] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        splitter = QSplitter(Qt.Horizontal, self)
        layout.addWidget(splitter, 1)

        # ---------------------------------------------------------- editor
        editor = QWidget(splitter)
        form_layout = QVBoxLayout(editor)
        form_layout.setContentsMargins(0, 0, 8, 0)
        kind_row = QFormLayout()
        self.kind_box = QComboBox(editor)
        for kind in waves.KINDS:
            self.kind_box.addItem(kind.capitalize() if kind != waves.DC else "DC", kind)
        kind_row.addRow("Kind", self.kind_box)
        form_layout.addLayout(kind_row)
        self.pages = QStackedWidget(editor)
        form_layout.addWidget(self.pages, 1)

        # standard shapes
        page = QWidget(self.pages)
        form = QFormLayout(page)
        self.frequency = self._spin(page, 0.001, 1e9, 1000, " Hz", 3)
        self.amplitude = self._spin(page, -100, 100, 1, " V", 4)
        self.offset = self._spin(page, -100, 100, 0, " V", 4)
        self.duty = self._spin(page, 0, 100, 50, " %", 1)
        self.phase = self._spin(page, -360, 360, 0, " °", 1)
        form.addRow("Frequency", self.frequency)
        form.addRow("Amplitude (peak)", self.amplitude)
        form.addRow("Offset", self.offset)
        form.addRow("Duty cycle", self.duty)
        form.addRow("Phase", self.phase)
        self.modulation = QComboBox(page)
        for title, key in MODULATIONS:
            self.modulation.addItem(title, key)
        form.addRow("Modulation", self.modulation)
        self.sweep_start = self._spin(page, 0.001, 1e9, 100, " Hz", 3)
        self.sweep_stop = self._spin(page, 0.001, 1e9, 10_000, " Hz", 3)
        self.sweep_time = self._spin(page, 1e-6, 1e4, 1, " s", 4)
        self.burst_cycles = QSpinBox(page)
        self.burst_cycles.setRange(1, 1_000_000)
        self.burst_period = self._spin(page, 1e-6, 1e4, 0.01, " s", 6)
        form.addRow("Sweep from", self.sweep_start)
        form.addRow("Sweep to", self.sweep_stop)
        form.addRow("Sweep time", self.sweep_time)
        form.addRow("Burst cycles", self.burst_cycles)
        form.addRow("Burst period", self.burst_period)
        self.standard_form = form
        self.pages.addWidget(page)

        # arbitrary points
        page = QWidget(self.pages)
        form = QFormLayout(page)
        self.formula = QLineEdit("sin(2*pi*x) + 0.3*sin(6*pi*x)", page)
        self.formula.setToolTip("One pass: x runs from 0 to 1; numpy functions and pi are known")
        form.addRow("Formula", self.formula)
        self.points = QSpinBox(page)
        self.points.setRange(2, 1 << 20)
        self.points.setValue(waves.DEFAULT_POINTS)
        form.addRow("Points", self.points)
        self.arbitrary_frequency = self._spin(page, 0.001, 1e9, 1000, " Hz", 3)
        form.addRow("Frequency", self.arbitrary_frequency)
        self.csv_button = QPushButton("Points from CSV...", page)
        set_icon(self.csv_button, "import")
        self.csv_button.clicked.connect(self.load_csv)
        form.addRow("", self.csv_button)
        self.source_label = QLabel(page)
        set_role(self.source_label, "hint")
        form.addRow("", self.source_label)
        self.pages.addWidget(page)

        # pattern
        page = QWidget(self.pages)
        pattern_layout = QVBoxLayout(page)
        form = QFormLayout()
        self.rate = self._spin(page, 1, 1e10, 1_000_000, " Hz", 0)
        form.addRow("Sample rate", self.rate)
        self.repeat = QSpinBox(page)
        self.repeat.setRange(0, 1_000_000)
        self.repeat.setSpecialValueText("until stopped")
        form.addRow("Passes", self.repeat)
        pattern_layout.addLayout(form)
        hint = QLabel("One line per pin: <code>D0: l5;h5;</code> (SDL: h/l samples, {…}n repeats, "
                      "b/B bytes, s\"text\" with the groups 0 and 1)", page)
        hint.setWordWrap(True)
        set_role(hint, "hint")
        pattern_layout.addWidget(hint)
        self.tracks_edit = QPlainTextEdit(page)
        self.tracks_edit.setPlainText("D0: {l5,h5,}4;\nD1: l10;h20;l10;")
        pattern_layout.addWidget(self.tracks_edit, 1)
        self.pages.addWidget(page)

        splitter.addWidget(editor)

        # ------------------------------------------------- preview, output
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.preview = WavePreview(right)
        right_layout.addWidget(self.preview, 1)
        self.summary = QLabel(right)
        set_role(self.summary, "hint")
        self.summary.setWordWrap(True)
        right_layout.addWidget(self.summary)
        output_box = QFrame(right)
        set_role(output_box, "card")
        output_layout = QHBoxLayout(output_box)
        output_layout.addWidget(QLabel("Output", output_box))
        self.output_box = QComboBox(output_box)
        self.output_box.setMinimumWidth(220)
        output_layout.addWidget(self.output_box, 1)
        self.start_button = QPushButton("Start", output_box)
        set_icon(self.start_button, "play")
        set_variant(self.start_button, "primary")
        self.start_button.clicked.connect(self.start)
        output_layout.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop", output_box)
        set_icon(self.stop_button, "stop")
        self.stop_button.clicked.connect(self.stop)
        output_layout.addWidget(self.stop_button)
        right_layout.addWidget(output_box)
        self.problems_label = QLabel(right)
        self.problems_label.setWordWrap(True)
        self.problems_label.setStyleSheet(f"color: {token('run.error')}")
        right_layout.addWidget(self.problems_label)
        self.limit_button = QPushButton("Limit the rate", right)
        self.limit_button.setToolTip("Play every n-th sample at a rate the output can (edges closer than that get lost)")
        self.limit_button.clicked.connect(self.limit_rate)
        right_layout.addWidget(self.limit_button, 0, Qt.AlignLeft)
        splitter.addWidget(right)
        splitter.setSizes([380, 620])

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(150)
        self._update_timer.timeout.connect(self.rebuild)
        for widget in (self.frequency, self.amplitude, self.offset, self.duty, self.phase, self.sweep_start,
                       self.sweep_stop, self.sweep_time, self.burst_period, self.arbitrary_frequency, self.rate):
            widget.valueChanged.connect(self._edited)
        for widget in (self.burst_cycles, self.points, self.repeat):
            widget.valueChanged.connect(self._edited)
        self.kind_box.currentIndexChanged.connect(self._kind_changed)
        self.modulation.currentIndexChanged.connect(self._edited)
        self.formula.textChanged.connect(self._edited)
        self.tracks_edit.textChanged.connect(self._edited)
        self.output_box.currentIndexChanged.connect(self._check_output)

        self.refresh_outputs()
        if waveform is not None:
            self.set_waveform(waveform)
        else:
            self.rebuild()
        self._dirty = False

    @staticmethod
    def _spin(parent: QWidget, low: float, high: float, value: float, suffix: str, decimals: int) -> QDoubleSpinBox:
        box = QDoubleSpinBox(parent)
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setValue(value)
        box.setSuffix(suffix)
        return box

    # ------------------------------------------------------------ identity
    @property
    def title(self) -> str:
        if self._path:
            return os.path.basename(self._path)
        if self.waveform is not None and self.waveform.name and self.waveform.name != "Waveform":
            return self.waveform.name
        if getattr(self, "_untitled", None) is None:
            from .base import untitled_title

            self._untitled = untitled_title("waveform")
        return self._untitled

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ---------------------------------------------------------- the form
    def kind(self) -> str:
        return self.kind_box.currentData()

    def _kind_changed(self) -> None:
        kind = self.kind()
        self.pages.setCurrentIndex(2 if kind == waves.PATTERN else 1 if kind == waves.ARBITRARY else 0)
        self._edited()

    def _edited(self, *_args) -> None:
        if self._filling:
            return
        self._dirty = True
        self._update_rows()
        self._update_timer.start()
        self.document_changed.emit()

    def _update_rows(self) -> None:
        modulation = self.modulation.currentData()
        for widget, visible in ((self.sweep_start, modulation == waves.SWEEP), (self.sweep_stop, modulation == waves.SWEEP),
                                (self.sweep_time, modulation == waves.SWEEP),
                                (self.burst_cycles, modulation == waves.BURST),
                                (self.burst_period, modulation == waves.BURST),
                                (self.duty, self.kind() in (waves.SQUARE, waves.PULSE))):
            widget.setVisible(visible)
            label = self.standard_form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

    def build(self) -> waves.Waveform:
        """The waveform of the form (raises :class:`~openscilab.core.waveform.WaveformError`)."""
        kind = self.kind()
        if kind == waves.PATTERN:
            return waves.pattern_from_sdl(parse_tracks(self.tracks_edit.toPlainText()), self.rate.value(),
                                          repeat=self.repeat.value())
        if kind == waves.ARBITRARY:
            if not self.formula.text().strip() and self._points is not None:
                # points of a file or a capture (an empty formula keeps them)
                return waves.Waveform(kind=waves.ARBITRARY, points=self._points.points, source=self._points.source,
                                      name=self._points.name, frequency=self.arbitrary_frequency.value())
            return waves.from_formula(self.formula.text(), self.arbitrary_frequency.value(), self.points.value())
        return waves.Waveform(
            kind=kind, frequency=self.frequency.value(), amplitude=self.amplitude.value(), offset=self.offset.value(),
            duty=self.duty.value() / 100, phase=self.phase.value(), modulation=self.modulation.currentData(),
            sweep_start=self.sweep_start.value(), sweep_stop=self.sweep_stop.value(), sweep_time=self.sweep_time.value(),
            burst_cycles=self.burst_cycles.value(), burst_period=self.burst_period.value())

    def rebuild(self) -> None:
        try:
            self.waveform, self.error = self.build(), ""
        except waves.WaveformError as error:
            self.waveform, self.error = None, str(error)
        self.preview.set_waveform(self.waveform, self.error)
        self.summary.setText(self.waveform.describe() if self.waveform is not None else self.error)
        self._check_output()

    def set_waveform(self, waveform: waves.Waveform) -> None:
        """Show ``waveform`` in the form."""
        self._filling = True
        try:
            self.kind_box.setCurrentIndex(self.kind_box.findData(waveform.kind))
            self.pages.setCurrentIndex(2 if waveform.is_pattern else 1 if waveform.kind == waves.ARBITRARY else 0)
            if waveform.is_pattern:
                self.rate.setValue(waveform.rate)
                self.repeat.setValue(waveform.repeat)
                self.tracks_edit.setPlainText(tracks_text(waveform))
            elif waveform.kind == waves.ARBITRARY:
                self.arbitrary_frequency.setValue(waveform.frequency)
                if waveform.formula:
                    self.formula.setText(waveform.formula)
                    self.points.setValue(len(waveform.points) if waveform.points is not None else waves.DEFAULT_POINTS)
                else:
                    self.formula.setText("")
                    self._points = waveform
                    self.source_label.setText(f"{len(waveform.points)} points" +
                                              (f" from {os.path.basename(waveform.source)}" if waveform.source else ""))
            else:
                self.frequency.setValue(waveform.frequency)
                self.amplitude.setValue(waveform.amplitude)
                self.offset.setValue(waveform.offset)
                self.duty.setValue(waveform.duty * 100)
                self.phase.setValue(waveform.phase)
                self.modulation.setCurrentIndex(max(self.modulation.findData(waveform.modulation), 0))
                self.sweep_start.setValue(waveform.sweep_start)
                self.sweep_stop.setValue(waveform.sweep_stop)
                self.sweep_time.setValue(waveform.sweep_time)
                self.burst_cycles.setValue(waveform.burst_cycles)
                self.burst_period.setValue(waveform.burst_period)
        finally:
            self._filling = False
        self.waveform = waveform
        self._update_rows()
        self.preview.set_waveform(waveform)
        self.summary.setText(waveform.describe())
        self._check_output()
        self.document_changed.emit()

    def load_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Points from CSV", "", "CSV files (*.csv);;All files (*)")
        if path:
            self.use_csv(path)

    def use_csv(self, path: str) -> bool:
        try:
            waveform = waves.from_csv(path)
        except (waves.WaveformError, OSError) as error:
            messages.error(self, "Points from CSV", "The file could not be read.", str(error))
            return False
        self.set_waveform(waveform)
        self._dirty = True
        self.document_changed.emit()
        return True

    # ------------------------------------------------------------- outputs
    def refresh_outputs(self) -> None:
        current = self.output_box.currentData()
        self.output_box.blockSignals(True)
        self.output_box.clear()
        for instrument in self.hub.instruments():
            generator = instrument.facet(GeneratorFacet)
            if generator is None:
                continue
            for info in generator.outputs():
                self.output_box.addItem(f"{instrument.name} · {info.name} ({info.kind})", (instrument.name, info.name))
        index = self.output_box.findData(current) if current is not None else -1
        if index < 0 and self.waveform is not None:
            index = self._fitting_output()
        self.output_box.setCurrentIndex(max(index, 0))
        self.output_box.blockSignals(False)
        self._check_output()

    def _fitting_output(self) -> int:
        kind = "pattern" if self.waveform.is_pattern else "analog"
        for index in range(self.output_box.count()):
            if self.output_box.itemText(index).endswith(f"({kind})"):
                return index
        return -1

    def selected_output(self):
        """``(instrument, generator, OutputInfo)`` of the chosen output, ``None`` without one."""
        data = self.output_box.currentData()
        if data is None:
            return None
        instrument = self.hub.find(data[0])
        generator = instrument.facet(GeneratorFacet) if instrument is not None else None
        if generator is None:
            return None
        return instrument, generator, generator.output(data[1])

    def problems(self) -> list[str]:
        if self.waveform is None:
            return [self.error] if self.error else []
        selected = self.selected_output()
        if selected is None:
            return ["No generator output: connect an instrument with a generator (e.g. sim:dho924s or sim:free)."]
        return waves.problems(self.waveform, selected[2])

    def _check_output(self) -> None:
        found = self.problems()
        self.problems_label.setText("\n".join(found))
        selected = self.selected_output()
        playing = self.playing is not None
        self.start_button.setEnabled(not found)
        self.start_button.setText("Restart" if playing else "Start")
        self.stop_button.setEnabled(playing)
        too_fast = selected is not None and self.waveform is not None and self.waveform.is_pattern \
            and self.waveform.rate > selected[2].max_rate
        self.limit_button.setVisible(bool(too_fast))
        if too_fast:
            factor = int(np.ceil(self.waveform.rate / selected[2].max_rate))
            self.limit_button.setText(f"Limit the rate: every {factor}. sample "
                                      f"({units.format_quantity(self.waveform.rate / factor, 'Hz')})")

    def limit_rate(self) -> bool:
        """A pattern faster than the output: keep every n-th sample (the time stays the same)."""
        selected = self.selected_output()
        if selected is None or self.waveform is None or not self.waveform.is_pattern:
            return False
        limited = waves.limit_rate(self.waveform, selected[2].max_rate)
        if limited is self.waveform:
            return False
        self.set_waveform(limited)
        self._dirty = True
        return True

    def start(self) -> bool:
        if self.waveform is None:
            return False
        selected = self.selected_output()
        found = self.problems()
        if found or selected is None:
            messages.warning(self, "Generator", "The waveform cannot be played on this output.", "\n".join(found))
            return False
        instrument, generator, info = selected
        try:
            generator.start(info.name, self.waveform)
        except InstrumentError as error:
            messages.warning(self, "Generator", str(error))
            return False
        self.playing = (instrument, info.name)
        self._check_output()
        self.output_changed.emit()
        return True

    def stop(self) -> None:
        if self.playing is None:
            return
        instrument, output = self.playing
        self.playing = None
        generator = instrument.facet(GeneratorFacet)
        if generator is not None and instrument in self.hub:
            try:
                generator.stop(output)
            except InstrumentError:
                pass
        self._check_output()
        self.output_changed.emit()

    # --------------------------------------------------------------- files
    def can_save(self) -> bool:
        return True

    def save(self) -> bool:
        if not self._path or not self._path.endswith(".wave.yaml"):
            return self.save_as()
        return self._write(self._path)

    def save_as(self) -> bool:
        start = self._path or f"{self.title.removesuffix('.wave')}.wave.yaml"
        if start.endswith(".sdl"):
            start = start[:-4] + ".wave.yaml"
        path, _ = QFileDialog.getSaveFileName(self, "Save waveform", start, WAVE_FILE_FILTER)
        if not path:
            return False
        if not path.endswith(".wave.yaml"):
            path += ".wave.yaml"
        return self._write(path)

    def _write(self, path: str) -> bool:
        if self.waveform is None:
            messages.warning(self, "Save waveform", "The waveform has an error.", self.error)
            return False
        try:
            waves.save(self.waveform, path)
        except OSError as error:
            messages.error(self, "Save waveform", f"{os.path.basename(path)} could not be written.", str(error))
            return False
        self._path = path
        self._dirty = False
        self.document_changed.emit()
        return True

    def shutdown(self) -> None:
        self.stop()
        self.bridge.close()


def open_waveform_file(path: str, hub: Hub) -> WaveformDocument:
    """A waveform document of a ``*.wave.yaml`` or ``*.sdl`` file (raises on errors)."""
    return WaveformDocument(hub, waves.load(path), path=path)
