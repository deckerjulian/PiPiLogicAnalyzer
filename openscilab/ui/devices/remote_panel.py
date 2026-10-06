# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The *Remote* tab of the device card of a remote device: the latest value of every input with its
time, a field for every output, a button for every command and how well the device's clock is
known (method, accuracy, round trip, drift). Refreshed twice a second."""

from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core import units
from ...core.instrument import InstrumentError
from ...driver.remote.instrument import RemoteFacet
from ...lab.engine.runtime import summarize
from ..theme import set_role

METHODS = {"network": "measured over the network", "signal": "sync signal", "none": "not measured yet"}


def _seconds(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return "-"
    return units.format_quantity(value, "s", 3)


#: how the clock of a simulated remote device runs (``timescale`` of its address)
SIM_CLOCKS = {"": "Its own clock: an offset and a drift", "utc": "Follows UTC (as with PTP or GPS)",
              "tai": "Follows TAI (as with PTP)"}


class RemotePanel(QScrollArea):
    """A remote device: its clock, inputs, outputs and commands; for a simulated one also the
    timings it simulates (network, drift, its clock)."""

    #: a simulated device runs with other settings now: its new address (``remote-sim:...?latency=20``)
    address_changed = Signal(str)
    #: a simulated device is to be started again with this address (another clock)
    restart_requested = Signal(str)

    def __init__(self, remote: RemoteFacet, parent: Optional[QWidget] = None, address: str = "") -> None:
        super().__init__(parent)
        self.remote = remote
        self.address = address
        self.setWidgetResizable(True)
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 10, 0, 0)
        self.state_label = QLabel(content)
        set_role(self.state_label, "hint")
        layout.addWidget(self.state_label)

        clock = QGroupBox("Clock of the device", content)
        clock_form = QFormLayout(clock)
        self.clock_labels: dict[str, QLabel] = {}
        for key in ("Method", "Accuracy", "Round trip", "Jitter", "Drift", "Measurements"):
            label = QLabel("-", clock)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.clock_labels[key] = label
            clock_form.addRow(key, label)
        layout.addWidget(clock)
        if remote.simulated is not None and address.startswith("remote-sim:"):
            layout.addWidget(self._build_simulation(content))

        inputs = QGroupBox("Inputs", content)
        self.input_form = QFormLayout(inputs)
        self.input_labels: dict[str, QLabel] = {}
        for name, item in remote.inputs().items():
            label = QLabel("-", inputs)
            label.setToolTip(item.get("description") or item["kind"])
            self.input_labels[name] = label
            self.input_form.addRow(f"{name} ({item['kind']})", label)
        layout.addWidget(inputs)

        outputs = QGroupBox("Outputs", content)
        output_form = QFormLayout(outputs)
        self.output_editors: dict[str, QWidget] = {}
        for name, item in remote.outputs().items():
            if item["kind"] == "bool":
                editor = QCheckBox(outputs)
                editor.setChecked(bool(item.get("default")))
                editor.toggled.connect(lambda checked, name=name: self._set(name, checked))
            else:
                editor = QLineEdit(outputs)
                editor.setPlaceholderText(str(item.get("default") if item.get("default") is not None else "")
                                          + (f" {item['unit']}" if item.get("unit") else ""))
                editor.returnPressed.connect(lambda name=name, editor=editor: self._set(name, editor.text()))
            editor.setToolTip(item.get("description") or "")
            self.output_editors[name] = editor
            output_form.addRow(name + (f" [{item['unit']}]" if item.get("unit") else ""), editor)
        if not remote.outputs():
            output_form.addRow(QLabel("none", outputs))
        layout.addWidget(outputs)

        commands = QGroupBox("Commands", content)
        command_layout = QVBoxLayout(commands)
        self.command_buttons: dict[str, QPushButton] = {}
        for name, item in remote.commands().items():
            row = QHBoxLayout()
            button = QPushButton(name, commands)
            button.setToolTip(item.get("description") or "")
            button.clicked.connect(lambda _checked=False, name=name: self._call(name))
            row.addWidget(button)
            row.addStretch(1)
            command_layout.addLayout(row)
            self.command_buttons[name] = button
        if not remote.commands():
            command_layout.addWidget(QLabel("none", commands))
        self.answer_label = QLabel("", commands)
        self.answer_label.setWordWrap(True)
        command_layout.addWidget(self.answer_label)
        layout.addWidget(commands)
        layout.addStretch(1)
        self.setWidget(content)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        remote = self.remote
        connected = remote.connected
        self.state_label.setText("connected" if connected else "not connected - the device has to connect again")
        state = remote.clock_state()
        if state is not None:
            self.clock_labels["Method"].setText(METHODS.get(state.method, state.method))
            self.clock_labels["Accuracy"].setText(f"± {_seconds(state.uncertainty)} (at most)")
            self.clock_labels["Round trip"].setText(_seconds(state.delay))
            self.clock_labels["Jitter"].setText(_seconds(state.jitter))
            self.clock_labels["Drift"].setText(f"{state.drift * 1e6:+.1f} ppm")
            self.clock_labels["Measurements"].setText(f"{state.samples}" + (
                f", {state.jumps} jump(s) of its clock" if state.jumps else ""))
        now = time.monotonic()
        for name, label in self.input_labels.items():
            latest = remote.latest(name)
            if latest is None:
                label.setText("- (sent only while a flow listens)" if remote.inputs()[name]["kind"] in ("analog", "digital")
                              else "-")
                continue
            if latest.samples is not None:
                text = f"{len(latest.samples)} samples @ {units.format_quantity(latest.rate, 'Hz')}"
            else:
                text = summarize(latest.value)
                unit = remote.inputs()[name].get("unit")
                if unit and isinstance(latest.value, (int, float)) and not isinstance(latest.value, bool):
                    from ...lab.nodes.remote import scalar_value

                    text = units.format_quantity(scalar_value(latest.value, unit), unit, 4)
            label.setText(f"{text}    ({now - latest.time:.1f} s ago)")

    # ------------------------------------------------------------ simulation
    def _build_simulation(self, parent: QWidget) -> QGroupBox:
        """What the simulated device does to its time: the network, the drift of its clock, whether
        its clock follows a time scale, whether its sample clock is shared."""
        from ...driver.remote.simulated import parse

        box = QGroupBox("Simulation: timing", parent)
        box.setToolTip("Network, clock and sample clock of the simulated device - try how well openSciLab finds "
                       "the time of its values")
        form = QFormLayout(box)
        self.sim_spins: dict[str, QDoubleSpinBox] = {}
        for key, title, unit, high, tip in (
                ("latency", "Network latency", " ms", 5000.0,
                 "one way, as on a LAN (0.2 ms), WiFi (2-10 ms) or the internet (20-100 ms)"),
                ("jitter", "Network jitter", " ms", 5000.0, "how much a single delay varies"),
                ("drift", "Clock drift", " ppm", 10_000.0,
                 "how much faster the device's clock runs (crystal: 10-100 ppm)")):
            spin = QDoubleSpinBox(box)
            spin.setRange(-high if key == "drift" else 0.0, high)
            spin.setDecimals(2 if key != "drift" else 1)
            spin.setSuffix(unit)
            spin.setToolTip(tip)
            self.sim_spins[key] = spin
            form.addRow(title, spin)
        self.sim_apply_button = QPushButton("Apply", box)
        self.sim_apply_button.setToolTip("At once, while the device runs")
        self.sim_apply_button.clicked.connect(self.apply_simulation)
        form.addRow(self.sim_apply_button)
        self.sim_clock_box = QComboBox(box)
        for key, title in SIM_CLOCKS.items():
            self.sim_clock_box.addItem(title, key)
        form.addRow("Clock", self.sim_clock_box)
        demo_name, _instance, options = parse(self.address)
        self.sim_shared_box = QCheckBox("Sample clock shared with openSciLab's instruments (no drift of its "
                                        "samples, only an offset)", box)
        self.sim_shared_box.setVisible(demo_name == "daq")
        form.addRow(self.sim_shared_box)
        self.sim_restart_button = QPushButton("Start it again with this clock", box)
        self.sim_restart_button.setToolTip("The clock and the sample clock are chosen when the device starts")
        self.sim_restart_button.clicked.connect(self.restart_simulation)
        form.addRow(self.sim_restart_button)
        self.sim_label = QLabel(box)
        self.sim_label.setWordWrap(True)
        set_role(self.sim_label, "hint")
        form.addRow(self.sim_label)
        settings = self.remote.simulated.settings()
        self.sim_spins["latency"].setValue(settings["latency"] * 1e3)
        self.sim_spins["jitter"].setValue(settings["jitter"] * 1e3)
        if settings["drift"] is None:
            self.sim_spins["drift"].setEnabled(False)
            self.sim_spins["drift"].setToolTip("The clock follows a time scale: it has no drift of its own")
        else:
            self.sim_spins["drift"].setValue(settings["drift"] * 1e6)
        self.sim_clock_box.setCurrentIndex(max(self.sim_clock_box.findData(options.get("timescale", "")), 0))
        self.sim_shared_box.setChecked(str(options.get("shared_clock", "")) in ("1", "true", "yes"))
        return box

    def _options(self, **changes) -> dict:
        from ...driver.remote.simulated import parse

        _demo, _instance, options = parse(self.address)
        options.update(changes)
        return {key: value for key, value in options.items() if value not in ("", None, "0", False)}

    def apply_simulation(self) -> bool:
        """Network and drift at once; the address keeps them (a reconnect, the next session)."""
        from ...driver.remote.simulated import address, parse

        latency, jitter = self.sim_spins["latency"].value(), self.sim_spins["jitter"].value()
        drift = self.sim_spins["drift"].value() if self.sim_spins["drift"].isEnabled() else None
        try:
            self.remote.simulated.configure(latency=latency / 1e3, jitter=jitter / 1e3,
                                            drift=drift / 1e6 if drift is not None else None)
        except ValueError as error:
            self.sim_label.setText(str(error))
            return False
        changes = {"latency": latency, "jitter": jitter}
        if drift is not None:
            changes["drift"] = drift
        demo_name, instance, _options = parse(self.address)
        self.address = address(demo_name, instance, self._options(**changes))
        self.address_changed.emit(self.address)
        self.sim_label.setText(f"Network {latency:g} ms ± {jitter:g} ms" + (
            f", clock drift {drift:+g} ppm" if drift is not None else "") + " - watch the clock above follow it.")
        return True

    def restart_simulation(self) -> str:
        """Start the device again with the chosen clock (its address says it)."""
        from ...driver.remote.simulated import address, parse

        demo_name, instance, _options = parse(self.address)
        changes = {"timescale": self.sim_clock_box.currentData() or None,
                   "shared_clock": "1" if self.sim_shared_box.isChecked() and demo_name == "daq" else None,
                   "latency": self.sim_spins["latency"].value(), "jitter": self.sim_spins["jitter"].value()}
        if not changes["timescale"]:
            changes["drift"] = self.sim_spins["drift"].value()
        else:
            changes["drift"] = None
        self.address = address(demo_name, instance, self._options(**changes))
        self.restart_requested.emit(self.address)
        return self.address

    def _set(self, name: str, value) -> None:
        try:
            if isinstance(value, str):
                kind = self.remote.outputs()[name]["kind"]
                value = float(units.parse(value)) if kind == "scalar" else value
            self.remote.set(name, value)
            self.answer_label.setText(f"{name} = {value}")
        except (InstrumentError, ValueError, units.UnitError) as error:
            self.answer_label.setText(f"{name}: {error}")

    def _call(self, name: str) -> None:
        try:
            self.answer_label.setText(f"{name}: {summarize(self.remote.call(name))}")
        except InstrumentError as error:
            self.answer_label.setText(f"{name}: {error}")
