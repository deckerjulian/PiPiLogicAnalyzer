# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""*Settings…* (Cmd+, on macOS): appearance, startup, navigation, data, devices and remote devices.

The values live in :mod:`openscilab.core.preferences`. *OK* stores what changed (``changes``);
the shell applies it. A new theme applies after a restart, which the dialog offers.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

from ...core import preferences
from ..icons import icon
from .common import button_box, dialog_layout, hint

PAGES = (("Appearance", "palette"), ("Startup", "home"), ("Navigation", "zoom-in"), ("Data", "folder"),
         ("Devices", "devices"), ("Remote devices", "wifi"), ("Time", "clock"))
HOST_CLOCK_LABELS = {"free": "Nothing (no time scale)", "ntp": "NTP (UTC, about milliseconds)",
                     "ptp": "PTP (UTC/TAI, microseconds)"}
THEME_LABELS = {"system": "Like the system", "dark": "Dark", "light": "Light"}
WHEEL_LABELS = {"zoom": "Zooms (Cmd/Ctrl + wheel moves)", "scroll": "Moves (Cmd/Ctrl + wheel zooms)"}


class PreferencesDialog(QDialog):
    """The settings of the application; ``changes`` after *OK*: what the user changed."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(620, 400)
        self.values = preferences.values()
        self.changes: dict[str, Any] = {}
        layout = dialog_layout(self)
        row = QHBoxLayout()
        self.pages = QListWidget(self)
        self.pages.setFixedWidth(150)
        for title, icon_name in PAGES:
            self.pages.addItem(title)
            self.pages.item(self.pages.count() - 1).setIcon(icon(icon_name))
        row.addWidget(self.pages)
        self.stack = QStackedWidget(self)
        row.addWidget(self.stack, 1)
        layout.addLayout(row, 1)
        self.pages.currentRowChanged.connect(self.stack.setCurrentIndex)

        self.theme = QComboBox(self)
        for key, label in THEME_LABELS.items():
            self.theme.addItem(label, key)
        self.font_size = QSpinBox(self)
        self.font_size.setRange(0, 24)
        self.font_size.setSpecialValueText("Large (12 px)")
        self.font_size.setSuffix(" px")
        self._page("Appearance", [("Theme", self.theme), ("Text size", self.font_size)],
                   "A new theme applies when openSciLab starts again. The waveform display stays dark, "
                   "like the screen of an instrument.")

        self.show_start = QCheckBox("Show the start page", self)
        self.restore = QCheckBox("Open the saved documents of the last session again", self)
        self.reconnect = QCheckBox("Connect the devices of the last session again", self)
        self._page("Startup", [("", self.show_start), ("", self.restore), ("", self.reconnect)],
                   "Saved files and the project come back; unsaved documents never do. When nothing was saved "
                   "openSciLab starts empty. With the devices their cards and data views come back as well; a "
                   "device that is not there is reported in the device list.")

        self.wheel = QComboBox(self)
        for key, label in WHEEL_LABELS.items():
            self.wheel.addItem(label, key)
        self.invert = QCheckBox("Turn the zoom direction around", self)
        self._page("Navigation", [("Mouse wheel", self.wheel), ("", self.invert)],
                   "For the flow graph and the waveform. A trackpad always moves with two fingers and zooms "
                   "when you pinch.")

        folder_row = QWidget(self)
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder = QLineEdit(self)
        self.folder.setPlaceholderText(preferences.default_folder())
        folder_layout.addWidget(self.folder, 1)
        browse = QPushButton("Choose...", self)
        browse.clicked.connect(self._choose_folder)
        folder_layout.addWidget(browse)
        self.recent_count = QSpinBox(self)
        self.recent_count.setRange(1, 50)
        self._page("Data", [("Folder", folder_row), ("Recent entries", self.recent_count)],
                   "New projects and save dialogs without a project start there (a flow that is not "
                   "saved yet writes its data into a temporary folder).")

        self.refresh = QDoubleSpinBox(self)
        self.refresh.setRange(0.5, 60)
        self.refresh.setSuffix(" s")
        self.refresh.setDecimals(1)
        self.simulators = QCheckBox("List the simulators with the devices", self)
        self.device_process = QCheckBox("Read devices on USB in a process of their own", self)
        self.device_process.setToolTip("Pico, Arduino and DSLogic: nothing the application does (drawing, flows, "
                                       "scripts) can delay reading them, so a stream does not overflow. "
                                       "Applies to devices connected from now on.")
        self.simulator_process = QCheckBox("Run simulators of USB devices in a process of their own", self)
        self.simulator_process.setToolTip(
            "A simulator that emulates a USB link (sim:pico, sim:daq) runs as the device it simulates: in a "
            "process of its own, with its processor load in the device list. Simulators that know the time of "
            "their samples, those of flows and simulators wired to each other stay in the application. Applies "
            "to simulators connected from now on.")
        import sys

        self.high_priority = QCheckBox("Read devices with a higher priority", self)
        self.high_priority.setToolTip(
            "The device processes run above the other programs, so a busy computer does not make a stream "
            "overflow. " + ("Windows allows it without asking." if sys.platform == "win32" else
                            "macOS and Linux ask for the administrator's password when openSciLab starts (on "
                            "Linux it can be allowed for good)."))
        self._page("Devices", [("Look for devices every", self.refresh), ("", self.simulators),
                               ("", self.device_process), ("", self.simulator_process), ("", self.high_priority)],
                   "Unplugged devices are noticed when the device list looks again. A device read in a process "
                   "of its own shows it on its card (Details).")

        self.remote_enabled = QCheckBox("Accept remote devices (scripts with openscilab_device)", self)
        self.remote_port = QSpinBox(self)
        self.remote_port.setRange(1024, 65535)
        token_row = QWidget(self)
        token_layout = QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        self.remote_token = QLineEdit(self)
        self.remote_token.setReadOnly(True)
        self.remote_token.setPlaceholderText("made when remote devices are switched on")
        token_layout.addWidget(self.remote_token, 1)
        new_token = QPushButton("New", self)
        new_token.setToolTip("A new token: devices with the old one are refused")
        new_token.clicked.connect(self._new_token)
        token_layout.addWidget(new_token)
        copy_token = QPushButton("Copy", self)
        copy_token.clicked.connect(lambda: QApplication.clipboard().setText(self.remote_token.text()))
        token_layout.addWidget(copy_token)
        self.remote_beacon = QCheckBox("Tell devices in the local network where openSciLab is", self)
        self.remote_example = QLineEdit(self)
        self.remote_example.setReadOnly(True)
        self.remote_example.setToolTip("Starts a demo device on the other computer (copy the folder "
                                       "openscilab_device there first)")
        self.remote_enabled.toggled.connect(self._remote_changed)
        self.remote_port.valueChanged.connect(self._remote_changed)
        self._page("Remote devices", [("", self.remote_enabled), ("Port", self.remote_port), ("Token", token_row),
                                      ("", self.remote_beacon), ("Try it", self.remote_example)],
                   "A script on another computer becomes a device: it connects to this port with the token and "
                   "describes its inputs, outputs and commands. openSciLab measures the clock of the device all "
                   "the time, so its values line up with those of the other instruments. See docs/remote.md.")

        self.host_clock = QComboBox(self)
        for key in preferences.HOST_CLOCKS:
            self.host_clock.addItem(HOST_CLOCK_LABELS[key], key)
        self.host_accuracy = QDoubleSpinBox(self)
        self.host_accuracy.setRange(0.0, 1000.0)
        self.host_accuracy.setDecimals(3)
        self.host_accuracy.setSuffix(" ms")
        self.host_accuracy.setSpecialValueText("usual for it")
        self.latencies = QLabel(self)
        self.latencies.setWordWrap(True)
        forget = QPushButton("Forget the measured latencies", self)
        forget.clicked.connect(self._forget_latencies)
        self._page("Time", [("This computer's clock follows", self.host_clock), ("Accuracy", self.host_accuracy),
                            ("Measured latencies", self.latencies), ("", forget)],
                   "Devices that stamp their samples in a shared time scale (PTP, GPS) are converted without "
                   "measuring when this computer's clock follows it too (ptp4l/phc2sys, chrony, Windows time). "
                   "Latencies measured with a loopback (node timing.calibrate) are used by the captures and "
                   "streams of the same instrument at the same rate. See docs/timing.md.")
        self._show_latencies()

        buttons = button_box(self, "OK")
        self.reset_button = buttons.addButton("Defaults", buttons.ButtonRole.ResetRole)
        self.reset_button.clicked.connect(lambda: self._load(dict(preferences.DEFAULTS)))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._load(self.values)
        self.pages.setCurrentRow(0)

    def _page(self, title: str, rows: list, text: str) -> None:
        page = QWidget(self.stack)
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignLeft)
        for label, widget in rows:
            if label:
                form.addRow(label, widget)
            else:
                form.addRow(widget)
        form.addRow(hint(text, page))
        page.setObjectName(f"page-{title.lower()}")
        self.stack.addWidget(page)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder for data and projects",
                                                  self.folder.text() or preferences.default_folder())
        if folder:
            self.folder.setText(folder)

    def _load(self, values: dict[str, Any]) -> None:
        self.theme.setCurrentIndex(max(self.theme.findData(values["appearance.theme"]), 0))
        self.font_size.setValue(int(values["appearance.font_size"]))
        self.show_start.setChecked(values["startup.show_start_page"])
        self.restore.setChecked(values["startup.restore_session"])
        self.reconnect.setChecked(values["startup.reconnect_devices"])
        self.wheel.setCurrentIndex(max(self.wheel.findData(values["navigation.wheel"]), 0))
        self.invert.setChecked(values["navigation.invert_zoom"])
        self.folder.setText(values["data.folder"])
        self.recent_count.setValue(int(values["data.recent_count"]))
        self.refresh.setValue(float(values["devices.refresh_s"]))
        self.simulators.setChecked(values["devices.show_simulators"])
        self.device_process.setChecked(values["devices.process"])
        self.simulator_process.setChecked(values["devices.simulator_process"])
        self.high_priority.setChecked(values["devices.high_priority"])
        self.remote_port.setValue(int(values["remote.port"]))
        self.remote_token.setText(str(values["remote.token"]))
        self.remote_beacon.setChecked(values["remote.beacon"])
        self.remote_enabled.setChecked(values["remote.enabled"])
        self._remote_changed()
        self.host_clock.setCurrentIndex(max(self.host_clock.findData(values["timing.host_clock"]), 0))
        self.host_accuracy.setValue(float(values["timing.host_accuracy"]) * 1e3)

    def chosen(self) -> dict[str, Any]:
        return {
            "appearance.theme": self.theme.currentData(),
            "appearance.font_size": self.font_size.value(),
            "startup.show_start_page": self.show_start.isChecked(),
            "startup.restore_session": self.restore.isChecked(),
            "startup.reconnect_devices": self.reconnect.isChecked(),
            "navigation.wheel": self.wheel.currentData(),
            "navigation.invert_zoom": self.invert.isChecked(),
            "data.folder": self.folder.text().strip(),
            "data.recent_count": self.recent_count.value(),
            "devices.refresh_s": float(self.refresh.value()),
            "devices.show_simulators": self.simulators.isChecked(),
            "devices.process": self.device_process.isChecked(),
            "devices.simulator_process": self.simulator_process.isChecked(),
            "devices.high_priority": self.high_priority.isChecked(),
            "remote.enabled": self.remote_enabled.isChecked(),
            "remote.port": self.remote_port.value(),
            "remote.token": self.remote_token.text(),
            "remote.beacon": self.remote_beacon.isChecked(),
            "timing.host_clock": self.host_clock.currentData(),
            "timing.host_accuracy": self.host_accuracy.value() / 1e3,
        }

    def _show_latencies(self) -> None:
        from ...core import settings, timing

        data = settings.get_settings(timing.TIMING_FILE) or {}
        entries = (data.get("latency") or {}) if isinstance(data, dict) else {}
        rows = []
        for key, entry in sorted(entries.items()):
            kind, uri, mode, rate = (key.split("|") + ["", "", "", ""])[:4]
            rows.append(f"{kind or uri} ({mode}, {rate} Hz): {float(entry.get('value', 0)) * 1e3:.3f} ms "
                        f"± {float(entry.get('uncertainty', 0)) * 1e3:.3f} ms")
        self.latencies.setText("\n".join(rows) or "none")

    def _forget_latencies(self) -> None:
        from ...core import settings, timing

        settings.persist_settings(timing.TIMING_FILE, {"latency": {}})
        self._show_latencies()

    def _new_token(self) -> None:
        from ...driver.remote.servers import new_token

        self.remote_token.setText(new_token())
        self._remote_changed()

    def _remote_changed(self, *_args) -> None:
        """A token as soon as remote devices are on; the command that starts a demo device."""
        if self.remote_enabled.isChecked() and not self.remote_token.text():
            from ...driver.remote.servers import new_token

            self.remote_token.setText(new_token())
        import socket

        try:
            host = socket.gethostbyname(socket.gethostname())
        except OSError:
            host = "<this computer>"
        self.remote_example.setText(f"python -m openscilab_device.demo climate --server {host}:"
                                    f"{self.remote_port.value()} --token {self.remote_token.text()}")

    def _accept(self) -> None:
        self.changes = preferences.update(self.chosen())
        self.accept()
