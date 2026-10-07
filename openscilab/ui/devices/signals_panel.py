# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The *Signals* tab of a simulated instrument's device card: what the simulator simulates.

A scenario (UART, SPI, I²C, a counter, the C64 bus, a capture file, …) with its settings puts
signals on the channels; a double-click on a channel gives that channel a signal of its own.
*Apply* changes the device at once, also while it captures.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import yaml
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...driver.simulated import scenarios
from ...driver.simulated.circuit import make_source
from ..dialogs.common import InlineMessage, button_box, dialog_layout, hint
from ..icons import set_icon
from ..theme import TEXT_MUTED, set_role, set_variant

#: sources offered for a single channel: (type, title, example of its settings)
SOURCE_CHOICES = (
    ("square", "Square wave", "frequency: 1 kHz, duty: 0.5"),
    ("pulse", "Pulses", "period: 1 ms, width: 100 us"),
    ("uart", "UART", "text: Hello, baud: 9600"),
    ("spi", "SPI line", "line: clk, frequency: 1 MHz"),
    ("i2c", "I²C line", "line: scl, frequency: 1 MHz"),
    ("counter", "Counter bit", "frequency: 1 MHz, bit: 0"),
    ("noise", "Random levels", "frequency: 1 kHz"),
    ("constant", "Constant level", "level: 1"),
    ("c64", "C64 bus line", "line: A0"),
    ("file", "Channel of a capture file", "path: capture.lac, channel: 0"),
    ("sine", "Sine (analog)", "frequency: 1 kHz, amplitude: 1 V, offset: 1.65 V"),
    ("triangle", "Triangle (analog)", "frequency: 1 kHz, amplitude: 1 V, offset: 1.65 V"),
)


def settings_text(source: dict) -> str:
    """``key: value, key: value`` of a source description (without its type)."""
    return ", ".join(f"{key}: {value}" for key, value in source.items() if key != "type")


def parse_settings(text: str) -> dict:
    """The settings typed as ``key: value, key: value``."""
    text = text.strip()
    if not text:
        return {}
    data = yaml.safe_load("{" + text + "}")
    if not isinstance(data, dict):
        raise ValueError("write the settings as key: value, key: value")
    return data


class SourceDialog(QDialog):
    """The signal of one channel: a kind of source and its settings, or what the scenario says."""

    def __init__(self, net: str, current: Optional[dict], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Signal of {net}")
        self.setMinimumWidth(440)
        #: the chosen source description; ``None``: as the scenario says
        self.source: Optional[dict] = None
        layout = dialog_layout(self)
        form = QFormLayout()
        self.kind_box = QComboBox(self)
        self.kind_box.addItem("As the scenario says", None)
        for kind, title, _example in SOURCE_CHOICES:
            self.kind_box.addItem(title, kind)
        form.addRow("Signal", self.kind_box)
        self.settings_edit = QLineEdit(self)
        form.addRow("Settings", self.settings_edit)
        layout.addLayout(form)
        self.example_label = hint("", self)
        layout.addWidget(self.example_label)
        self.message = InlineMessage(self)
        layout.addWidget(self.message)
        buttons = button_box(self, "OK")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind_box.currentIndexChanged.connect(self._kind_changed)
        if current:
            self.kind_box.setCurrentIndex(max(self.kind_box.findData(current.get("type")), 0))
            self.settings_edit.setText(settings_text(current))
        self._kind_changed()

    def _kind_changed(self) -> None:
        kind = self.kind_box.currentData()
        example = next((text for name, _title, text in SOURCE_CHOICES if name == kind), "")
        self.settings_edit.setEnabled(kind is not None)
        self.settings_edit.setPlaceholderText(example)
        self.example_label.setText(f"For example: {example}. Empty: the usual values." if kind else
                                   "The channel carries what the scenario puts on it.")

    def _accept(self) -> None:
        kind = self.kind_box.currentData()
        if kind is None:
            self.source = None
            self.accept()
            return
        try:
            source = {"type": kind, **parse_settings(self.settings_edit.text())}
            make_source(source)  # checks the settings
        except (TypeError, ValueError, KeyError, yaml.YAMLError) as error:
            self.message.show_error(f"These settings do not work: {error}")
            return
        self.source = source
        self.accept()


class WireDialog(QDialog):
    """A wire between two pins of a simulator: an output to a channel (a loopback)."""

    def __init__(self, sources: list[str], targets: list[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a wire")
        layout = dialog_layout(self)
        layout.addWidget(hint("What the first pin does, the second one sees - e.g. an output wired to a channel "
                              "to measure the latency (Timing tab).", self))
        form = QFormLayout()
        self.source_box = QComboBox(self)
        self.source_box.addItems(sources)
        self.target_box = QComboBox(self)
        self.target_box.addItems(targets)
        if self.target_box.count() > 1:
            self.target_box.setCurrentIndex(1)
        form.addRow("From", self.source_box)
        form.addRow("To", self.target_box)
        layout.addLayout(form)
        layout.addWidget(button_box(self, "Add"))

    @property
    def wire(self) -> dict:
        return {"from": self.source_box.currentText(), "to": self.target_box.currentText()}


class SignalsPanel(QWidget):
    """Scenario, its settings and the signal of every channel of a simulated instrument; its wires
    and the USB link it emulates."""

    #: the signals were applied: (configuration, {channel index: suggested name})
    applied = Signal(object, object)
    #: wires or the USB link changed: {"wiring": [...]} or {"usb": {...} or False}
    circuit_changed = Signal(object)

    def __init__(self, simulation, parent: Optional[QWidget] = None,
                 on_names: Optional[Callable[[dict], Any]] = None) -> None:
        super().__init__(parent)
        #: the simulator's :class:`~openscilab.core.instrument.SimulationFacet` (here or in a device process)
        self.simulation = simulation
        self.on_names = on_names
        #: the nets of its channels (they do not change while it is open)
        self.digital_nets = list(simulation.channel_names())
        self.analog_nets = list(simulation.analog_channel_names())
        #: single channels set by hand: net -> source description
        self.single: dict[str, dict] = dict(simulation.signals().get("channels") or {})
        #: why the last wire could not be set ("" when it could)
        self.wire_error = ""
        self._editors: dict[str, QWidget] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("Simulates", self))
        self.scenario_box = QComboBox(self)
        self.scenario_box.setToolTip("What is connected to the channels of the simulator")
        for key, reason in simulation.scenarios():
            scenario = scenarios.SCENARIOS[key]
            self.scenario_box.addItem(scenario.title, scenario.key)
            index = self.scenario_box.count() - 1
            self.scenario_box.setItemData(index, reason or scenario.description, Qt.ToolTipRole)
            if reason:
                # shown, but not selectable: the tooltip says what it needs
                item = self.scenario_box.model().item(index)
                item.setEnabled(False)
                self.scenario_box.setItemText(index, f"{scenario.title} ({reason})")
        row.addWidget(self.scenario_box, 1)
        self.apply_button = QPushButton("Apply", self)
        set_variant(self.apply_button, "primary")
        set_icon(self.apply_button, "check")
        self.apply_button.setToolTip("Put these signals on the channels now")
        self.apply_button.clicked.connect(self.apply)
        row.addWidget(self.apply_button)
        layout.addLayout(row)

        self.description_label = hint("", self)
        layout.addWidget(self.description_label)
        self.form_holder = QWidget(self)
        self.form = QFormLayout(self.form_holder)
        self.form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.form_holder)
        self.names_box = QCheckBox("Name the channels after their signals (TX, SCL, A0, …) for the next capture",
                                   self)
        self.names_box.setChecked(True)
        layout.addWidget(self.names_box)
        self.message = InlineMessage(self)
        layout.addWidget(self.message)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Channel", "Pin", "Signal"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setToolTip("Double-click a channel to give it a signal of its own")
        self.table.cellDoubleClicked.connect(lambda row, _column: self.edit_channel(row))
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.edit_button = QPushButton("Set signal of the selected channel...", self)
        set_icon(self.edit_button, "pencil")
        self.edit_button.setToolTip("Give the channel selected in the table a signal of its own, instead of the "
                                    "one of the scenario (or double-click the channel)")
        self.edit_button.clicked.connect(lambda: self.edit_channel(self.table.currentRow()))
        self.edit_button.setEnabled(False)  # (until a channel is selected)
        self.table.itemSelectionChanged.connect(
            lambda: self.edit_button.setEnabled(bool(self.table.selectionModel().selectedRows())))
        footer.addWidget(self.edit_button)
        footer.addWidget(hint("What the device drives itself (outputs, a generator, its wiring) stays as it is.",
                              self), 1)
        layout.addLayout(footer)
        layout.addLayout(self._build_circuit())

        self.scenario_box.currentIndexChanged.connect(self._scenario_changed)
        self.reload()

    # ------------------------------------------------------- wires and USB
    def _build_circuit(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        wires = QGroupBox("Wires", self)
        wires.setToolTip("Pins of the simulator wired to each other, e.g. an output to a channel (a loopback "
                         "to measure the latency)")
        wires_layout = QVBoxLayout(wires)
        self.wire_table = QTableWidget(0, 2, wires)
        self.wire_table.setHorizontalHeaderLabels(["From", "To"])
        self.wire_table.verticalHeader().setVisible(False)
        self.wire_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.wire_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.wire_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.wire_table.setMaximumHeight(110)
        wires_layout.addWidget(self.wire_table)
        buttons = QHBoxLayout()
        self.add_wire_button = QPushButton("Add wire...", wires)
        set_icon(self.add_wire_button, "plus")
        self.add_wire_button.clicked.connect(self.add_wire_dialog)
        buttons.addWidget(self.add_wire_button)
        self.remove_wire_button = QPushButton("Remove", wires)
        set_icon(self.remove_wire_button, "trash")
        self.remove_wire_button.clicked.connect(lambda: self.remove_wire(self.wire_table.currentRow()))
        buttons.addWidget(self.remove_wire_button)
        buttons.addStretch(1)
        wires_layout.addLayout(buttons)
        row.addWidget(wires, 1)

        usb = QGroupBox("USB link and clock", self)
        usb.setToolTip("Blocks arrive as over USB: in frames, late and uneven - the time of the samples is "
                       "found as with the real device (latency, sync signal)")
        form = QFormLayout(usb)
        self.usb_box = QCheckBox("Emulate a USB link", usb)
        form.addRow(self.usb_box)
        self.usb_spins: dict[str, QDoubleSpinBox] = {}
        for key, title in (("latency", "Latency"), ("jitter", "Jitter"), ("frame", "Frame")):
            spin = QDoubleSpinBox(usb)
            spin.setRange(0.0, 1000.0)
            spin.setDecimals(3)
            spin.setSuffix(" ms")
            spin.setSingleStep(0.1)
            self.usb_spins[key] = spin
            form.addRow(title, spin)
        self.drift_spin = QDoubleSpinBox(usb)
        self.drift_spin.setRange(-10_000.0, 10_000.0)
        self.drift_spin.setDecimals(1)
        self.drift_spin.setSuffix(" ppm")
        self.drift_spin.setToolTip("How much faster the sample clock of the device runs than the computer's "
                                   "(a crystal: 10-100 ppm); found by a sync signal or the arrivals of a long stream")
        form.addRow("Clock drift", self.drift_spin)
        self.usb_apply_button = QPushButton("Apply", usb)
        set_icon(self.usb_apply_button, "check")
        self.usb_apply_button.clicked.connect(self.apply_usb)
        form.addRow(self.usb_apply_button)
        self.usb_box.toggled.connect(lambda on: [spin.setEnabled(on) for spin in self.usb_spins.values()])
        row.addWidget(usb)
        self.refresh_circuit()
        return row

    def wire_sources(self) -> list[str]:
        """Pins a wire may start at: every pin of the device (outputs, PWM) and the channels."""
        return list(dict.fromkeys(self.simulation.pin_names() + self.nets()))

    def profile_wires(self) -> list[dict]:
        """The wires the profile of the simulator brings (not removable here)."""
        return self.simulation.profile_wiring()

    def refresh_circuit(self) -> None:
        wires = self.simulation.wiring()
        built_in = self.profile_wires()
        self.wire_table.setRowCount(len(wires) + len(built_in))
        for row, wire in enumerate(wires + built_in):
            fixed = row >= len(wires)
            for column, key in enumerate(("from", "to")):
                item = QTableWidgetItem(str(wire[key]) + ("   · of the profile" if fixed and column == 1 else ""))
                if fixed:
                    item.setForeground(QColor(TEXT_MUTED))
                    item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
                    item.setToolTip("A wire of the simulator's profile: always there (a wire added to the same "
                                    "pin replaces it)")
                self.wire_table.setItem(row, column, item)
        self.remove_wire_button.setEnabled(bool(wires))
        usb = self.simulation.usb()
        self.usb_box.setChecked(usb is not None)
        values = usb or {"frame": 0.001, "latency": 0.001, "jitter": 0.0003}
        for key, spin in self.usb_spins.items():
            spin.setValue(float(values[key]) * 1e3)
            spin.setEnabled(usb is not None)
        self.drift_spin.setValue(self.simulation.drift() * 1e6)

    def set_wires(self, wires: list[dict]) -> bool:
        try:
            self.simulation.set_wiring(wires)
        except ValueError as error:
            self.wire_error = str(error)
            self.message.show_error(self.wire_error)
            return False
        self.wire_error = ""
        self.refresh_circuit()
        self.refresh_table()
        self.circuit_changed.emit({"wiring": self.simulation.wiring()})
        self.message.show_success("Wires: " + (", ".join(f"{wire['from']} → {wire['to']}" for wire in wires)
                                               or "none"))
        return True

    def add_wire(self, source: str, target: str) -> bool:
        wires = [wire for wire in self.simulation.wiring() if wire["to"] != target]  # (one source per pin)
        return self.set_wires(wires + [{"from": source, "to": target}])

    def add_wire_dialog(self) -> bool:
        dialog = WireDialog(self.wire_sources(), self.nets(), self)
        if not dialog.exec():
            return False
        return self.add_wire(dialog.wire["from"], dialog.wire["to"])

    def remove_wire(self, row: int) -> bool:
        wires = self.simulation.wiring()
        if not 0 <= row < len(wires):
            return False
        del wires[row]
        return self.set_wires(wires)

    def apply_usb(self) -> bool:
        try:
            if self.usb_box.isChecked():
                self.simulation.set_usb({key: spin.value() / 1e3 for key, spin in self.usb_spins.items()})
            else:
                self.simulation.set_usb(None)
        except ValueError as error:
            self.message.show_error(str(error))
            return False
        drift = self.drift_spin.value()
        if abs(drift - self.simulation.drift() * 1e6) > 1e-6:
            self.simulation.set_drift(drift)
        usb = self.simulation.usb()
        self.circuit_changed.emit({"usb": usb if usb is not None else False, "drift": drift})
        self.message.show_success("USB link: " + (f"latency {usb['latency'] * 1e3:g} ms, jitter "
                                                  f"{usb['jitter'] * 1e3:g} ms, frames of {usb['frame'] * 1e3:g} ms"
                                                  if usb else "none - the simulator knows the time of its samples")
                                 + f"; clock drift {drift:+g} ppm")
        return True

    # ---------------------------------------------------------------- state
    def nets(self) -> list[str]:
        return self.digital_nets + self.analog_nets

    def reload(self) -> None:
        """Show what the device simulates now."""
        config = self.simulation.signals()
        self.single = dict(config.get("channels") or {})
        self.scenario_box.blockSignals(True)
        self.scenario_box.setCurrentIndex(max(self.scenario_box.findData(config["scenario"]), 0))
        self.scenario_box.blockSignals(False)
        self._build_form(config)
        self.refresh_table()

    def _scenario_changed(self) -> None:
        self._build_form({})
        self.message.clear()

    def _build_form(self, values: dict) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._editors.clear()
        scenario = scenarios.SCENARIOS[self.scenario_box.currentData() or "default"]
        self.description_label.setText(scenario.description)
        for param in scenario.params:
            value = values.get(param.name, param.default)
            if param.kind == "channel":
                editor: QWidget = QSpinBox(self.form_holder)
                editor.setRange(1, max(len(self.digital_nets), 1))
                editor.setValue(int(value))
            elif param.kind == "path":
                editor = QWidget(self.form_holder)
                line = QHBoxLayout(editor)
                line.setContentsMargins(0, 0, 0, 0)
                edit = QLineEdit(str(value), editor)
                edit.setPlaceholderText("A capture (.lac)")
                line.addWidget(edit, 1)
                browse = QPushButton("Choose...", editor)
                browse.clicked.connect(lambda _checked=False, edit=edit: self._choose_file(edit))
                line.addWidget(browse)
                editor.edit = edit
            else:
                editor = QLineEdit(str(value), self.form_holder)
                editor.returnPressed.connect(self.apply)
            editor.setObjectName(f"param-{param.name}")
            self._editors[param.name] = editor
            self.form.addRow(param.label, editor)
        self.form_holder.setVisible(bool(scenario.params))

    def _choose_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Capture to play", edit.text(), "Captures (*.lac *.lac.gz)")
        if path:
            edit.setText(path)

    def configuration(self) -> dict:
        """What the tab says now (not applied yet)."""
        config: dict[str, Any] = {"scenario": self.scenario_box.currentData() or "default"}
        for name, editor in self._editors.items():
            if isinstance(editor, QSpinBox):
                config[name] = editor.value()
            elif isinstance(editor, QLineEdit):
                config[name] = editor.text()
            else:
                config[name] = editor.edit.text()
        config["channels"] = dict(self.single)
        return config

    # -------------------------------------------------------------- actions
    def apply(self) -> bool:
        try:
            config, names = self.simulation.apply_signals(self.configuration())
        except scenarios.ScenarioError as error:
            self.message.show_error(str(error))
            return False
        self.message.show_success(f"The device simulates: {scenarios.describe(config)}")
        self.refresh_table()
        if self.names_box.isChecked() and names and self.on_names is not None:
            self.on_names(names)
        self.applied.emit(config, names)
        return True

    def edit_channel(self, row: int) -> bool:
        nets = self.nets()
        if not 0 <= row < len(nets):
            return False
        net = nets[row]
        dialog = SourceDialog(net, self.single.get(net), self)
        if not dialog.exec():
            return False
        if dialog.source is None:
            self.single.pop(net, None)
        else:
            self.single[net] = dialog.source
        return self.apply()

    def refresh_table(self) -> None:
        described = self.simulation.circuit()
        nets = self.nets()
        digital = len(self.digital_nets)
        self.table.setRowCount(len(nets))
        for row, net in enumerate(nets):
            channel = str(row + 1) if row < digital else f"A{row - digital + 1}"
            signal = described.get(net, "low (nothing connected)")
            if net in self.single:
                signal += "   · set by hand"
            for column, text in enumerate((channel, net, signal)):
                item = QTableWidgetItem(text)
                if column == 2 and net not in described:
                    item.setForeground(QColor(TEXT_MUTED))
                self.table.setItem(row, column, item)
        set_role(self.description_label, "hint")
