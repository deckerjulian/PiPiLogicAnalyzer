# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The controls that capture with an instrument, for the data view that shows the captures.

:class:`CaptureControls` makes the widgets once – the quick settings (rate, length, trigger),
*Settings…* (all settings: apply them or capture), *Profiles*, *Capture*, *Capture again*,
*Stop*, the state of the device, a summary of the settings of the next capture and the channel
table – and binds them to the capture controller of an instrument (:meth:`bind`), or to none.
The data view puts the bar into its capture tool bar and the summary and the channels into its
*Capture* tab.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...core import units
from ..icons import set_icon
from ..theme import TEXT_MUTED, set_role, set_variant, token
from ..widgets.quick_capture import QuickCaptureBar


class CaptureControls(QObject):
    """Capture widgets bound to one capture controller at a time."""

    def __init__(self, parent: QWidget, repeat=None, stop=None) -> None:
        """``repeat``, ``stop``: what *Again* and *Stop* do instead of asking the controller (the data
        view also stops a recording)."""
        super().__init__(parent)
        self._parent = parent
        self.controller = None
        self.hub = None
        self.quick_capture = QuickCaptureBar(parent)
        self.quick_capture.settings_requested.connect(self.capture_settings)
        self.quick_capture.changed.connect(self.update)
        self.settings_button = QPushButton("Settings...", parent)
        set_variant(self.settings_button, "tool")
        set_icon(self.settings_button, "gear")
        self.settings_button.setToolTip("Channels and their names, rate, trigger, acquisition: apply them, or capture")
        self.settings_button.clicked.connect(self.capture_settings)
        self.profiles_button = QPushButton("Profiles", parent)
        set_variant(self.profiles_button, "tool")
        set_icon(self.profiles_button, "bookmark")
        self.profiles_menu = QMenu(self.profiles_button)
        self.profiles_menu.aboutToShow.connect(self._fill_profiles)
        self.profiles_button.setMenu(self.profiles_menu)
        self.capture_button = QPushButton("Capture", parent)
        set_variant(self.capture_button, "primary")
        set_icon(self.capture_button, "record")
        self.capture_button.setToolTip("Capture with these settings (F5)")
        self.capture_button.clicked.connect(self.start_capture)
        self.repeat_button = QPushButton("Again", parent)
        set_variant(self.repeat_button, "tool")
        set_icon(self.repeat_button, "repeat")
        self.repeat_button.setToolTip("Capture again with the settings of the last capture (Ctrl+R)")
        self.repeat_button.clicked.connect(repeat or self.repeat_capture)
        self.stop_button = QPushButton("Stop", parent)
        set_variant(self.stop_button, "danger")
        set_icon(self.stop_button, "stop")
        self.stop_button.setToolTip("Stop the running capture (Shift+F5)")
        self.stop_button.clicked.connect(stop or self.stop_capture)
        self.state_label = QLabel(parent)
        set_role(self.state_label, "hint")
        self.summary_label = QLabel(parent)
        self.summary_label.setWordWrap(True)
        set_role(self.summary_label, "hint")
        self.channel_table = QTableWidget(0, 4, parent)
        self.channel_table.setHorizontalHeaderLabels(["Channel", "Pin", "Name", "Captured"])
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.channel_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.channel_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.bind(None)

    # ------------------------------------------------------------- binding
    def bind(self, controller, hub=None) -> None:
        """Capture with ``controller`` (``None``: no instrument - the controls are off)."""
        old = self.controller
        if old is controller:
            self.update()
            return
        if old is not None:
            if old.quick_settings == self.quick_capture.capture_session:
                old.quick_settings = None
            for signal, slot in ((old.changed, self.update), (old.state_changed, self.update_state),
                                 (old.profile_loaded, self.quick_capture.reload)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.controller, self.hub = controller, hub
        if controller is not None:
            self.quick_capture.set_driver(controller.driver)
            controller.quick_settings = self.quick_capture.capture_session
            controller.changed.connect(self.update)
            controller.state_changed.connect(self.update_state)
            controller.profile_loaded.connect(self.quick_capture.reload)
            self.quick_capture.reload()
        self.update()

    def release(self) -> None:
        self.bind(None)

    @property
    def parent_widget(self) -> QWidget:
        """The parent of the dialogs: the window of the controls - a dialog that is a child of a tool
        bar takes over its style (a transparent background: it showed white)."""
        return self._parent.window()

    # ----------------------------------------------------------- capturing
    def capture_settings(self) -> bool:
        """All settings: *Use settings* keeps them, *Start capture* captures."""
        if self.controller is None:
            return False
        started = self.controller.start_capture(self.parent_widget)
        self.quick_capture.reload()
        self.update()
        return started

    def start_capture(self) -> bool:
        return self.controller is not None and self.controller.quick_start_capture(self.parent_widget)

    def repeat_capture(self) -> bool:
        return self.controller is not None and bool(self.controller.repeat_capture())

    def stop_capture(self) -> None:
        if self.controller is not None:
            self.controller.abort()

    def _fill_profiles(self) -> None:
        self.profiles_menu.clear()
        if self.controller is not None:
            self.controller.fill_profiles_menu(self.profiles_menu, self.parent_widget)

    # ------------------------------------------------------------- showing
    @property
    def connected(self) -> bool:
        controller = self.controller
        if controller is None:
            return False
        return self.hub is None or controller.instrument in self.hub

    def update(self) -> None:
        controller = self.controller
        connected = self.connected
        capturing = connected and controller.is_capturing
        for widget in (self.quick_capture, self.settings_button, self.capture_button, self.profiles_button):
            widget.setEnabled(connected and not capturing)
        self.repeat_button.setEnabled(connected and not capturing and controller.last_session is not None)
        self.stop_button.setEnabled(capturing)
        self.update_state()
        self.summary_label.setText(self.summary())
        self.fill_channels()

    def update_state(self) -> None:
        """The state: ready, armed, receiving n %, failed (and the power of the board)."""
        controller = self.controller
        if controller is None:
            self.state_label.setText("")
            return
        text, kind = controller.state_text()
        colours = {"idle": "device.disconnected", "armed": "device.busy", "busy": "device.busy",
                   "error": "device.error"}
        state = f"<span style='color:{token(colours[kind])}'>●</span> {html.escape(text)}"
        if controller.power_text:
            state += f"  ·  power {html.escape(controller.power_text)}"
        self.state_label.setText(state)
        self.state_label.setToolTip(text)

    def summary(self) -> str:
        if self.controller is None:
            return "No device: choose one above to capture (or open a capture file)."
        session = self.controller.settings()
        if session is None:
            return "No settings yet: choose rate and length, or open Settings."
        parts = [units.format_quantity(session.frequency, "Hz"),
                 f"{session.pre_trigger_samples + session.post_trigger_samples:,} samples",
                 f"{len(session.capture_channels)} channels"]
        if session.analog_channels:
            parts.append(f"{len(session.analog_channels)} analog")
        parts.append(f"trigger: {session.trigger_type.label.lower()}")
        if getattr(session, "acquisition_mode", "") == "stream":
            parts.append("stream")
        return " · ".join(parts)

    def fill_channels(self) -> None:
        controller = self.controller
        if controller is None:
            self.channel_table.setRowCount(0)
            return
        driver = controller.driver
        names = controller.channel_names()
        settings = controller.settings()
        captured = {channel.channel_number for channel in settings.capture_channels} if settings else set()
        pins = {pin.channel: pin.name for pin in controller.instrument.pins() if pin.channel is not None}
        analog_names = controller.analog_names()
        analog_captured = {channel.channel_number for channel in settings.analog_channels} if settings else set()
        analog = list(driver.analog_channel_names()) if driver.analog_channel_count else []
        rows = [(f"D{number}" if not pins else f"{number}", pins.get(number, "–"), names.get(number, ""),
                 number in captured) for number in range(driver.channel_count)]
        rows += [(f"A{number}", label, analog_names.get(number, ""), number in analog_captured)
                 for number, label in enumerate(analog)]
        self.channel_table.setRowCount(len(rows))
        for row, (channel, pin, name, used) in enumerate(rows):
            for column, value in enumerate((channel, pin, name or "–", "✓" if used else "")):
                item = QTableWidgetItem(value)
                if column == 2 and not name:
                    item.setForeground(QColor(TEXT_MUTED))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.channel_table.setItem(row, column, item)
