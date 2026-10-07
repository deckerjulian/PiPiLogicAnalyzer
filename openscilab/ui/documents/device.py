# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The device card: an instrument of the hub as a document.

Header: name, status, what the device is (kind, address), the settings of the next capture,
*Capture...* (its data view), *Profiles* and *Disconnect*. Tabs, as far as the device has them:
**Remote** (remote devices), **Pins** (one table: pin, channel, name, capabilities and – with GPIO –
mode, value and actions; pin strip, monitor, *All outputs safe* (Esc), stimulus and capture),
**Signals** and **Events** (simulators), **Send** (protocol transmitters), **Timing** (how its samples
are placed) and **Details** (everything about the device and its functions: self-test, network,
restart, bootloader, firmware, reconnect, copy the details). Every action on the device is reported
(``action_performed``) so the shell can put a marker into running captures.
"""

from __future__ import annotations

import html
import logging
from collections import deque
from typing import Optional

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import firmware, units
from ...core.hub import EVENT_REMOVED, EVENT_STATUS, Hub
from ...core.instrument import (
    CacheFacet,
    MODE_ANALOG,
    MODE_INPUT,
    MODE_INPUT_PULLDOWN,
    MODE_INPUT_PULLUP,
    MODE_OUTPUT,
    MODE_PWM,
    PIN_ADC,
    PIN_DAC,
    PIN_DOUT,
    PIN_PULLDOWN,
    PIN_PULLUP,
    PIN_PWM,
    GeneratorFacet,
    Instrument,
    InstrumentError,
    InstrumentStatus,
    MonitorState,
    PinInfo,
)
from ...driver.base import CAPABILITY_RESTART, DeviceConnectionError, facets_of
from .. import messages
from ..devices.hub_bridge import HubBridge
from ..icons import icon, set_icon
from ..theme import BORDER, PANEL_LIGHT, TEXT, TEXT_MUTED, qcolor, set_role, set_variant, token
from .base import DocumentWidget

log = logging.getLogger(__name__)

HISTORY = 60
#: milliseconds between updates of the pin values while the monitor runs
STATE_UPDATE_MS = 33


def status_icon(status: InstrumentStatus, size: int = 10) -> QIcon:
    """A dot in the colour of the device status."""
    from PySide6.QtGui import QGuiApplication, QPixmap

    # sharp on high resolution screens: drawn at the pixel density of the screen
    screen = QGuiApplication.primaryScreen()
    ratio = max(screen.devicePixelRatio() if screen is not None else 1.0, 2.0)
    pixmap = QPixmap(int(size * 2 * ratio), int(size * 2 * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(token(status.token)))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(size // 2, size // 2, size, size)
    painter.end()
    return QIcon(pixmap)


def modes_of(pin: PinInfo) -> list[str]:
    modes = [MODE_INPUT]
    if PIN_PULLUP in pin.capabilities:
        modes.append(MODE_INPUT_PULLUP)
    if PIN_PULLDOWN in pin.capabilities:
        modes.append(MODE_INPUT_PULLDOWN)
    if PIN_DOUT in pin.capabilities:
        modes.append(MODE_OUTPUT)
    if PIN_PWM in pin.capabilities:
        modes.append(MODE_PWM)
    if PIN_ADC in pin.capabilities:
        modes.append(MODE_ANALOG)
    return modes


class PinStrip(QWidget):
    """The pins as a header row of the board: colour by state, click selects."""

    pin_clicked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.pins: list[PinInfo] = []
        self.levels: dict[str, Optional[int]] = {}
        self.modes: dict[str, str] = {}
        self.selected: Optional[str] = None
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_pins(self, pins: list[PinInfo]) -> None:
        self.pins = pins
        self.update()

    def cell(self, index: int) -> QRectF:
        width = max(min((self.width() - 8) / max(len(self.pins), 1), 46), 18)
        return QRectF(4 + index * width, 4, width - 4, 46)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for index, pin in enumerate(self.pins):
            rect = self.cell(index)
            reserved = not pin.usable
            mode = self.modes.get(pin.name, MODE_INPUT)
            level = self.levels.get(pin.name)
            fill = QColor(PANEL_LIGHT)
            if mode in (MODE_OUTPUT, MODE_PWM):
                fill = qcolor("pin.output")
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(token("run.running")) if pin.name == self.selected else QColor(BORDER), 1.5))
            painter.drawRoundedRect(rect, 4, 4)
            dot = QRectF(rect.center().x() - 5, rect.top() + 6, 10, 10)
            color = qcolor("pin.high") if level == 1 else qcolor("led.off")
            if reserved:
                color = qcolor("pin.idle")
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(dot)
            painter.setPen(QColor(TEXT_MUTED if reserved else TEXT))
            painter.drawText(QRectF(rect.left(), rect.top() + 20, rect.width(), 24), Qt.AlignCenter, pin.name)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        for index, pin in enumerate(self.pins):
            if self.cell(index).contains(event.position()):
                self.selected = pin.name
                self.pin_clicked.emit(pin.name)
                self.update()
                return


class Sparkline(QWidget):
    """A small history of a voltage."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.values: deque = deque(maxlen=HISTORY)
        self.setMinimumSize(80, 22)

    def add(self, value: float) -> None:
        self.values.append(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if len(self.values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        low, high = min(self.values), max(self.values)
        span = (high - low) or 1.0
        path = QPainterPath()
        for index, value in enumerate(self.values):
            x = index * (self.width() - 2) / (HISTORY - 1) + 1
            y = self.height() - 2 - (value - low) / span * (self.height() - 4)
            path.moveTo(x, y) if index == 0 else path.lineTo(x, y)
        painter.setPen(QPen(QColor(token("type.analog")), 1.2))
        painter.drawPath(path)


#: Columns of the pin table
COL_PIN, COL_CHANNEL, COL_NAME, COL_CAN, COL_MODE, COL_VALUE, COL_ACTION = range(7)
PIN_COLUMNS = ["Pin", "Channel", "Name", "Can", "Mode", "Value", "Action"]
PIN_WIDTHS = {COL_PIN: 48, COL_CHANNEL: 64, COL_NAME: 110, COL_CAN: 160, COL_MODE: 104, COL_VALUE: 84}


class DeviceDocument(DocumentWidget):
    """One instrument: pins (GPIO, monitor), signals and events (simulators), sending, timing, details.

    The header names the device and what it is, with *Capture...*, *Profiles* and *Disconnect*; the
    *Details* tab holds what concerns the device itself (self-test, network, bootloader, firmware,
    test signals of the board). The tabs follow what the device can do.
    """

    document_kind = "device"

    data_requested = Signal(object)
    disconnect_requested = Signal(object)
    #: open the device again (it was disconnected or unplugged)
    reconnect_requested = Signal(object)
    #: (instrument, text): something was set on the device (a marker for running captures)
    action_performed = Signal(object, str)
    #: record the monitor as a capture: (instrument, rate, pins, analog)
    record_requested = Signal(object)
    #: *Update firmware* of the instrument
    firmware_requested = Signal(object)
    #: open an example of the library (``13-time/01-align-instruments``)
    example_requested = Signal(str)
    #: the settings, at a page (``"Time"``)
    settings_requested = Signal(str)
    #: stimulus and capture: (capture instrument, this instrument, pin, pulse width in seconds)
    stimulus_requested = Signal(object, object, str, float)
    _monitor_state = Signal(object)
    _event = Signal(str)

    def __init__(self, instrument: Instrument, hub: Hub, parent: Optional[QWidget] = None,
                 controller=None) -> None:
        super().__init__(parent)
        self.instrument = instrument
        self.hub = hub
        #: the capture controller of an instrument that captures (:mod:`..devices.capture`)
        if controller is None and instrument.capture is not None:
            from ..devices.capture import capture_controller

            controller = capture_controller(instrument)
        self.controller = controller
        self.bridge = HubBridge(hub, self)
        self.bridge.changed.connect(self._on_hub_event)
        #: pins the user confirmed may drive their level
        self._confirmed: set[str] = set()
        self.history: dict[str, Sparkline] = {}
        self.value_labels: dict[str, QLabel] = {}
        self._remove_handler = None
        self._device_events = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 10)
        layout.setSpacing(10)
        self._build_header(layout)
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)

        self.pins_page = self._build_pins_page()
        self.tabs.addTab(self.pins_page, icon("chip"), "Pins")
        # (made only for a tab: a child left out of the layout showed as a black box in the corner)
        self.event_list: Optional[QListWidget] = None
        model = getattr(instrument, "simulated_driver", None)
        #: what a simulator simulates (scenario, signals of single channels); ``None`` for real devices
        self.signals_panel = None
        if model is not None:
            from ..devices.signals_panel import SignalsPanel

            self.signals_panel = SignalsPanel(model, self, on_names=self._name_channels)
            self.signals_panel.applied.connect(self._signals_applied)
            self.signals_panel.circuit_changed.connect(self._circuit_changed)
            self.tabs.addTab(self.signals_panel, icon("wave"), "Signals")
            self.event_list = QListWidget(self)
            self.tabs.addTab(self.event_list, icon("list"), "Events")
            self._device_events = lambda stamp, text: self._event.emit(f"{stamp:10.4f} s  {text}")
            model.add_event_listener(self._device_events)
        #: UART, SPI and I²C sent by the device itself (capabilities TX_*)
        self.send_panel = None
        generator = instrument.facet(GeneratorFacet)
        if generator is not None and any(generator.transmits(protocol) for protocol in ("uart", "spi", "i2c")):
            from ..devices.send_panel import SendPanel

            self.send_panel = SendPanel(instrument, self)
            self.tabs.addTab(self.send_panel, icon("arrow-right"), "Send")
        #: captures kept in the device's own cache (the bridge app of an oscilloscope)
        self.cache_panel = None
        cache = instrument.facet(CacheFacet)
        if cache is not None:
            from ..devices.cache_panel import CachePanel

            self.cache_panel = CachePanel(cache, self)
            self.tabs.addTab(self.cache_panel, icon("list"), "Cache")
        #: inputs, outputs, commands and clock of a remote device
        self.remote_panel = None
        from ...driver.remote.instrument import RemoteFacet

        remote = instrument.facet(RemoteFacet)
        if remote is not None:
            from ..devices.remote_panel import RemotePanel

            self.remote_panel = RemotePanel(remote, self, address=instrument.uri)
            self.remote_panel.address_changed.connect(self._address_changed)
            self.remote_panel.restart_requested.connect(self._restart_at)
            self.tabs.insertTab(0, self.remote_panel, icon("wifi"), "Remote")
            self.tabs.setCurrentIndex(0)
        #: the time of the samples and how to know it better (instruments that capture)
        self.timing_page = None
        if instrument.capture is not None:
            self.timing_page = self._build_timing_page()
            self.tabs.addTab(self.timing_page, icon("clock"), "Timing")
        self.tabs.addTab(self._build_details_page(), icon("info"), "Details")

        #: the pins the table was built for: other pins rebuild it, a status change does not
        self._pin_signature = None
        self._monitor_owned = False
        #: the monitor may be started and stopped elsewhere too (a recording): the box follows it
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(500)
        self.monitor_timer.timeout.connect(self.sync_monitor)
        self.monitor_timer.start()
        # a fast monitor reports hundreds of times a second: the card shows the newest state
        # some thirty times a second instead of repainting for every report
        self._pending_state: Optional[MonitorState] = None
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(STATE_UPDATE_MS)
        self._state_timer.timeout.connect(self._show_pending_state)
        self._monitor_state.connect(self._queue_state)
        # the time of the samples changes while flows capture: the tabs follow while they show
        self.details_page = self.tabs.widget(self.tabs.count() - 1)
        self._details_timer = QTimer(self)
        self._details_timer.setInterval(2000)
        self._details_timer.timeout.connect(self._update_shown_details)
        self._details_timer.start()
        self.tabs.currentChanged.connect(lambda _index: self._update_shown_details())
        if self.event_list is not None:
            self._event.connect(self.event_list.addItem)
        if self.controller is not None:
            self.controller.changed.connect(self._update_capture)
            self.controller.start_power_polling()
        self.refresh()

    # -------------------------------------------------------------- header
    def _build_header(self, layout: QVBoxLayout) -> None:
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        row = QHBoxLayout()
        self.name_label = QLabel(self)
        set_role(self.name_label, "title")
        # long names may be cut (the tooltip has them): the card fits beside a data view
        self.name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.name_label.setMinimumWidth(120)
        row.addWidget(self.name_label, 1)
        self.status_label = QLabel(self)
        row.addWidget(self.status_label)
        row.addStretch(1)
        titles.addLayout(row)
        self.kind_label = QLabel(self)
        set_role(self.kind_label, "hint")
        self.kind_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.kind_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        titles.addWidget(self.kind_label)
        header.addLayout(titles, 1)

        self.data_button = QPushButton("Capture...", self)
        set_icon(self.data_button, "record")
        set_variant(self.data_button, "primary")
        self.data_button.setToolTip("Capture with this device in a data view (settings, start, the captures)")
        self.data_button.clicked.connect(lambda: self.data_requested.emit(self.instrument))
        header.addWidget(self.data_button)
        # the capture profiles of the device: saved settings (and decoders) loaded at a click
        self.profiles_button = QPushButton("Profiles", self)
        set_icon(self.profiles_button, "bookmark")
        self.profiles_button.setToolTip("Capture profiles: load saved settings for this device, save the current "
                                        "ones, the standard profiles")
        self.profiles_menu = QMenu(self.profiles_button)
        self.profiles_menu.aboutToShow.connect(self._fill_profiles)
        self.profiles_button.setMenu(self.profiles_menu)
        header.addWidget(self.profiles_button)
        # what concerns the device itself: buttons on the Details tab (made here, placed there)
        controller = self.controller
        self.action_settings = QAction(icon("gear"), "Capture settings...", self)
        self.action_settings.setToolTip("Channels, rate, length and trigger of the next capture")
        self.action_self_test = QAction(icon("checklist"), "Self-test...", self)
        self.action_generated = QAction(icon("wave"), "Test signals of the board...", self)
        self.action_generated.setToolTip("Captures of test signals the board generates itself")
        self.action_network = QAction(icon("wifi"), "Network settings...", self)
        self.action_restart = QAction(icon("refresh"), "Restart device...", self)
        self.action_restart.setToolTip("Restart the device: outputs are released, what was set on it is gone")
        self.action_bootloader = QAction(icon("power"), "Restart into the bootloader...", self)
        self.action_bootloader.setToolTip("For a firmware update: the board appears as a USB drive")
        self.action_firmware = QAction(icon("chip"), "Update firmware...", self)
        self.action_reconnect = QAction(icon("plug"), "Reconnect", self)
        self.action_reconnect.setToolTip("Close the connection and open it again (an Arduino restarts when its "
                                         "port is opened)")
        self.action_copy = QAction(icon("copy"), "Copy the details", self)
        self.action_copy.setToolTip("Everything on this tab as text, e.g. for a bug report")
        self.device_actions = [self.action_settings, self.action_self_test, self.action_generated,
                               self.action_network, self.action_restart, self.action_bootloader,
                               self.action_firmware, self.action_reconnect, self.action_copy]
        self.action_restart.triggered.connect(self.restart_device)
        self.action_reconnect.triggered.connect(lambda: self.reconnect_requested.emit(self.instrument))
        self.action_copy.triggered.connect(self.copy_details)
        if controller is not None:
            self.action_settings.triggered.connect(lambda: controller.start_capture(self))
            self.action_self_test.triggered.connect(lambda: controller.run_board_test(self))
            self.action_generated.triggered.connect(lambda: controller.generated_capture(self))
            self.action_network.triggered.connect(lambda: controller.update_network_settings(self))
            self.action_bootloader.triggered.connect(lambda: controller.enter_bootloader(
                self, release=lambda: self.disconnect_requested.emit(self.instrument)))
        self.action_firmware.triggered.connect(lambda: self.firmware_requested.emit(self.instrument))
        self.disconnect_button = QPushButton("Disconnect", self)
        set_icon(self.disconnect_button, "unplug")
        self.disconnect_button.clicked.connect(lambda: self.disconnect_requested.emit(self.instrument))
        header.addWidget(self.disconnect_button)
        self.reconnect_button = QPushButton("Reconnect", self)
        set_icon(self.reconnect_button, "plug")
        set_variant(self.reconnect_button, "primary")
        self.reconnect_button.setToolTip("Open the device again")
        self.reconnect_button.clicked.connect(lambda: self.reconnect_requested.emit(self.instrument))
        self.reconnect_button.setVisible(False)
        header.addWidget(self.reconnect_button)
        layout.addLayout(header)
        # the settings of the next capture (a loaded profile shows here)
        self.capture_label = QLabel(self)
        set_role(self.capture_label, "hint")
        self.capture_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.capture_label)
        from ..dialogs.common import Banner

        # problems with the device (connect, capture, unplugged) show here instead of in a dialog
        self.banner = Banner("error", self)
        self.banner.setVisible(False)
        self.banner.button.clicked.connect(lambda: self.banner.setVisible(False))
        layout.addWidget(self.banner)

    def start_capture(self) -> bool:
        """Capture with this device: in its data view (the settings and the start are there)."""
        if self.controller is None:
            return False
        self.data_requested.emit(self.instrument)
        return True

    def document_actions(self) -> list:
        """What the card does, for the command palette and the list of shortcuts."""
        if getattr(self, "_palette_actions", None) is None:
            actions = []
            for text, shortcut, slot in (
                ("Capture in a data view", None, self.start_capture),
                ("All outputs safe", "Esc (Pins tab)", self.all_safe),
                ("Monitor the inputs", None, lambda: self.monitor_box.setChecked(not self.monitor_box.isChecked())),
                ("Disconnect", None, lambda: self.disconnect_requested.emit(self.instrument)),
            ):
                action = QAction(text, self)
                if shortcut:
                    action.setToolTip(shortcut)
                    action.setData(shortcut)  # shown in the list of shortcuts, works on the card
                action.triggered.connect(lambda _checked=False, slot=slot: slot())
                actions.append(action)
            self._palette_actions = actions
        return self._palette_actions

    # capturing happens in the data view (the header's Start captures there)
    runnable = False

    def channel_name(self, pin: PinInfo) -> str:
        """The name the capture settings give the channel of ``pin``."""
        if self.controller is None:
            return ""
        if pin.channel is not None:
            name = self.controller.channel_names().get(pin.channel)
            if name:
                return name
        if pin.analog_channel is not None:
            return self.controller.analog_names().get(pin.analog_channel, "")
        return ""

    def _fill_profiles(self) -> None:
        self.profiles_menu.clear()
        if self.controller is not None:
            self.controller.fill_profiles_menu(self.profiles_menu, self)

    def capture_summary(self) -> str:
        """The settings of the next capture in a line."""
        from ...core.device_summary import settings_line

        session = self.controller.settings() if self.controller is not None else None
        if session is None:
            return ""
        return "Next capture: " + settings_line(session)

    def _update_capture(self) -> None:
        """The device functions the device has (the buttons on the Details tab)."""
        controller = self.controller
        connected = self.instrument in self.hub and self.instrument.status != InstrumentStatus.DISCONNECTED
        self.action_reconnect.setVisible(bool(self.instrument.uri))
        self.action_copy.setVisible(True)
        self.profiles_button.setVisible(controller is not None)
        self.capture_label.setVisible(controller is not None)
        if controller is None:
            for action in (self.action_settings, self.action_self_test, self.action_generated, self.action_network,
                           self.action_restart, self.action_bootloader):
                action.setVisible(False)
            self._update_device_box()
            return
        driver = controller.driver
        capturing = connected and controller.is_capturing
        summary = self.capture_summary()
        self.capture_label.setText(summary)
        self.capture_label.setToolTip(summary)
        self.profiles_button.setEnabled(connected)
        for action, visible in ((self.action_settings, True),
                                (self.action_generated, connected and controller.can_simulate_on_board()),
                                (self.action_self_test, bool(driver.has_self_test)),
                                (self.action_network, bool(driver.supports_network_config)),
                                (self.action_restart, CAPABILITY_RESTART in driver.capabilities()),
                                (self.action_bootloader, bool(driver.supports_bootloader))):
            action.setVisible(visible)
            action.setEnabled(connected and not capturing)
        self._update_device_box()
        self._update_names()

    def restart_device(self) -> bool:
        """*Restart device*: asked first; the outputs are released, the card shows the device anew."""
        controller = self.controller
        if controller is None or controller.is_capturing:
            return False
        if not messages.confirm(self, "Restart device", f"Restart {self.instrument.name}?", "Restart",
                                "Outputs are released and what was set on the device (pin modes, PWM, a running "
                                "generator) is gone. The connection stays."):
            return False
        try:
            done = bool(controller.driver.restart())
        except Exception as error:  # noqa: BLE001 - every way a device can fail to answer
            log.debug("done = bool(controller.driver.restart()) failed: every way a device can fail to answer", exc_info=True)
            done = False
            self.show_banner(f"{self.instrument.name} could not be restarted: {error}", "error")
        if done:
            self._confirmed.clear()
            self.action_performed.emit(self.instrument, "restart")
            self.show_banner(f"{self.instrument.name} was restarted.", "info")
            self._pin_signature = None  # modes and levels are read again
            self.refresh()
        elif not self.banner.isVisible():
            self.show_banner(f"{self.instrument.name} did not restart. Unplug it and plug it in again.", "error")
        return done

    def details_text(self) -> str:
        """Everything of the Details tab as plain text (*Copy the details*)."""
        instrument = self.instrument
        lines = [instrument.name, ""]
        for title, rows in self._detail_sections():
            lines.append(title)
            lines += [f"  {name}: {value}" for name, value in rows]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def copy_details(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.details_text())
        self.show_banner("The details were copied to the clipboard.", "info")

    def _detail_sections(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """The sections of the Details tab: the instrument, what its driver describes, its
        capabilities, the time of its samples and its capture limits."""
        instrument = self.instrument
        sections = [(title, [(str(name), str(value)) for name, value in rows])
                    for title, rows in instrument.details() if title != "Timing"]
        capabilities = sorted(instrument.capabilities())
        sections.append(("Capabilities", [
            ("Facets", ", ".join(facet.title for facet in instrument.facets()) or "none"),
            ("Reported", ", ".join(capabilities) or "none"),
            ("Unlocks", ", ".join(sorted(facets_of(capabilities))) or "none")]))
        sections.append(("Time of the samples", self._timing_rows()))
        limits = self._limit_rows()
        if limits:
            sections.append(("Capture limits (samples)", [
                (name, f"pre-trigger {low} to {pre}, post-trigger up to {post}, total {total}")
                for name, low, pre, post, total in limits]))
        return sections

    def _timing_rows(self) -> list[tuple[str, str]]:
        from ...core import timing
        from ...core.instrument import timing_rows

        instrument = self.instrument
        state = instrument.timing.state() if getattr(instrument, "timing", None) is not None else None
        if state is not None:
            rows = [("Placed by" if name == "Method" else name, value) for name, value in timing_rows(state)]
        else:
            nothing = ("nothing captured yet (then: between the start command and the arrival of the samples, "
                       "unless something better is known)")
            rows = [("Placed by", nothing)]
        from ...core.timing_tools import CLOCKS, device_config

        rows.append(("Sample clock", CLOCKS[device_config(instrument)["clock"]]))
        session = self.controller.last_session if self.controller is not None else None
        timescale = getattr(session, "device_timescale", None) if session is not None else None
        rows.append(("Device time stamps", f"in {str(timescale).upper()} (PTP/GPS)" if timescale else
                     "none (the device does not stamp its samples in a time scale)"))
        stored = timing.latencies_of(instrument)
        rows.append(("Measured latencies", "; ".join(
            f"{title}: {units.format_quantity(latency.value, 's', 3)} ± "
            f"{units.format_quantity(latency.uncertainty, 's', 3)}" for title, latency in stored.items())
            or "none"))
        rows.append(("This computer's clock", timing.host_clock_text()))
        return rows

    def _limit_rows(self) -> list[tuple[str, str, str, str, str]]:
        """Samples a capture in the device's buffer can hold, by the number of channels."""
        controller = self.controller
        if controller is None or self.instrument not in self.hub:
            return getattr(self, "_limits", [])
        if getattr(self, "_limits", None) is None:
            from ...core.formatting import to_thousands

            driver = controller.driver
            rows = []
            try:
                for count in (8, 16, 24):
                    if count - 8 >= driver.channel_count:
                        break
                    limits = driver.get_limits(range(min(count, driver.channel_count)))
                    rows.append((f"{min(count, driver.channel_count)} channels", to_thousands(limits.min_pre_samples),
                                 to_thousands(limits.max_pre_samples), to_thousands(limits.max_post_samples),
                                 to_thousands(limits.max_total_samples)))
            except Exception:  # noqa: BLE001 - a device that cannot say: no table
                log.debug("_limit_rows: a device that cannot say: no table", exc_info=True)
                rows = []
            self._limits = rows
        return self._limits

    def _update_device_box(self) -> None:
        """The buttons of the functions the device has, three in a row."""
        box = getattr(self, "device_box", None)
        if box is None:
            return
        shown = [button for button in self.device_tool_buttons if button.defaultAction().isVisible()]
        for button in self.device_tool_buttons:
            self.device_grid.removeWidget(button)
            button.setVisible(button in shown)
        for index, button in enumerate(shown):
            self.device_grid.addWidget(button, index // 3, index % 3)
        box.setVisible(bool(shown))

    # ---------------------------------------------------------------- pins
    def _build_pins_page(self) -> QWidget:
        page = QWidget(self)
        # Esc: all outputs safe – on this tab only (elsewhere Esc closes popups and stays harmless)
        QShortcut(QKeySequence(Qt.Key_Escape), page, activated=self.all_safe, context=Qt.WidgetWithChildrenShortcut)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        tools = QHBoxLayout()
        self.monitor_box = QCheckBox("Monitor", page)
        self.monitor_box.setToolTip("Read the inputs periodically and show them here")
        self.monitor_box.toggled.connect(self.set_monitoring)
        tools.addWidget(self.monitor_box)
        self.monitor_rate = QSpinBox(page)
        self.monitor_rate.setRange(1, 1000)
        self.monitor_rate.setValue(20)
        self.monitor_rate.setSuffix(" /s")
        self.monitor_rate.setToolTip("Readings a second")
        # a new rate applies at once to the monitor this card started
        self.monitor_rate.valueChanged.connect(
            lambda _rate: self.set_monitoring(True) if self._monitor_owned and self.monitor_box.isChecked() else None)
        tools.addWidget(self.monitor_rate)
        self.record_button = QPushButton("Record", page)
        set_icon(self.record_button, "record")
        self.record_button.setToolTip("Record the monitor as a slow capture in a data view")
        self.record_button.clicked.connect(lambda: self.record_requested.emit(self.instrument))
        tools.addWidget(self.record_button)
        tools.addStretch(1)
        self.safe_button = QPushButton("All outputs safe", page)
        set_variant(self.safe_button, "danger")
        set_icon(self.safe_button, "stop")
        self.safe_button.setToolTip("Every output back to an input (Esc)")
        self.safe_button.clicked.connect(self.all_safe)
        tools.addWidget(self.safe_button)
        layout.addLayout(tools)
        self.strip = PinStrip(page)
        self.strip.pin_clicked.connect(self._select_pin)
        layout.addWidget(self.strip)
        self.pin_table = QTableWidget(0, len(PIN_COLUMNS), page)
        self.pin_table.setHorizontalHeaderLabels(PIN_COLUMNS)
        self.pin_table.verticalHeader().setVisible(False)
        self.pin_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pin_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pin_table.setTextElideMode(Qt.ElideRight)
        self.pin_table.setWordWrap(False)
        layout.addWidget(self.pin_table, 1)
        self.stimulus_row = QWidget(page)
        stimulus = QHBoxLayout(self.stimulus_row)
        stimulus.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Stimulus: capture with", self.stimulus_row)
        label.setToolTip("Arm a capture (also on another instrument), then pulse the selected pin")
        stimulus.addWidget(label)
        self.stimulus_target = QComboBox(self.stimulus_row)
        self.stimulus_target.setToolTip("Any connected instrument that captures (also another device)")
        stimulus.addWidget(self.stimulus_target, 1)
        self.stimulus_button = QPushButton("Arm and pulse", self.stimulus_row)
        self.stimulus_button.setToolTip("Start the capture, then pulse the selected pin once")
        set_icon(self.stimulus_button, "record")
        self.stimulus_button.clicked.connect(self._request_stimulus)
        stimulus.addWidget(self.stimulus_button)
        layout.addWidget(self.stimulus_row)
        return page

    def _build_details_page(self) -> QWidget:
        """Everything about the device in one place: what it can be asked to do, the time of its
        samples, its capture limits, its capabilities, board, firmware and connection."""
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(6)

        def heading(text: str, parent: QWidget = content) -> QLabel:
            label = QLabel(text, parent)
            set_role(label, "heading")
            return label

        def text_label() -> QLabel:
            label = QLabel(content)
            label.setTextFormat(Qt.RichText)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setWordWrap(True)
            return label

        # ------------------------------------------------------- the device
        self.device_box = QWidget(content)
        box_layout = QVBoxLayout(self.device_box)
        box_layout.setContentsMargins(0, 0, 0, 6)
        box_layout.addWidget(heading("Device", self.device_box))
        self.device_grid = QGridLayout()
        self.device_grid.setSpacing(6)
        self.device_tool_buttons = []
        for action in self.device_actions:
            button = QToolButton(self.device_box)
            button.setDefaultAction(action)  # enabled and text follow the action (visible: see below)
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.device_tool_buttons.append(button)
        box_layout.addLayout(self.device_grid)
        layout.addWidget(self.device_box)

        # ------------------------------------------------------ capture limits
        self.limits_heading = heading("Capture limits")
        self.limits_heading.setToolTip("Samples a capture in the buffer of the device can hold, by the number of "
                                       "channels")
        layout.addWidget(self.limits_heading)
        self.limits_table = QTableWidget(0, 5, content)
        self.limits_table.setHorizontalHeaderLabels(["Channels", "Min. pre-trigger", "Max. pre-trigger",
                                                     "Max. post-trigger", "Max. total"])
        self.limits_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.limits_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.limits_table.setFocusPolicy(Qt.NoFocus)
        self.limits_table.setShowGrid(False)
        self.limits_table.verticalHeader().setVisible(False)
        self.limits_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.limits_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.limits_table)

        # ------------------------------------- capabilities, board, connection
        layout.addWidget(heading("Capabilities"))
        self.capabilities_label = text_label()
        layout.addWidget(self.capabilities_label)
        layout.addWidget(heading("Connection and details"))
        self.details_label = text_label()
        layout.addWidget(self.details_label)
        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    # -------------------------------------------------------------- timing
    def _build_timing_page(self) -> QWidget:
        """The time of the samples: how they are placed and how well, the sample clock, measuring
        the latency with a loopback, a sync output, the clock of this computer."""
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(6)

        def heading(text: str) -> QLabel:
            label = QLabel(text, content)
            set_role(label, "heading")
            return label

        def note(text: str) -> QLabel:
            label = QLabel(text, content)
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            set_role(label, "hint")
            return label

        # ------------------------------------------------------------ state
        layout.addWidget(heading("Time of the samples"))
        self.timing_label = QLabel(content)
        self.timing_label.setTextFormat(Qt.RichText)
        self.timing_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.timing_label.setWordWrap(True)
        layout.addWidget(self.timing_label)
        layout.addWidget(note(
            "Samples are placed on this computer's clock - between the start command and their arrival, unless "
            "something better is known: a <b>latency</b> measured below, a <b>sync signal</b> every device records "
            "(flow nodes <i>timing.sync</i> and <i>timing.align</i>), a <b>shared sample clock</b>, or a device and "
            "a computer that both follow <b>PTP, GPS or NTP</b>."))
        row = QHBoxLayout()
        self.time_settings_button = QPushButton("This computer's clock...", content)
        set_icon(self.time_settings_button, "gear")
        self.time_settings_button.setToolTip("Settings → Time: whether this computer's clock follows NTP or PTP "
                                             "(ptp4l/phc2sys, chrony), and how well")
        self.time_settings_button.clicked.connect(lambda: self.settings_requested.emit("Time"))
        row.addWidget(self.time_settings_button)
        self.align_button = QPushButton("Align with a sync signal (example)...", content)
        set_icon(self.align_button, "wave")
        self.align_button.setToolTip("Opens the example flow: a sync signal recorded by two instruments aligns them")
        self.align_button.clicked.connect(lambda: self.example_requested.emit("13-time/01-align-instruments"))
        row.addWidget(self.align_button)
        row.addStretch(1)
        layout.addLayout(row)

        # ----------------------------------------------------- sample clock
        layout.addWidget(heading("Sample clock"))
        self.clock_box = QComboBox(content)
        from ...core.timing_tools import CLOCKS

        for key, title in CLOCKS.items():
            self.clock_box.addItem(title, key)
        self.clock_box.setToolTip("Shared or external: the sample clock of this device comes from another one "
                                  "(a reference clock, the clock output of an instrument); aligning it with a "
                                  "sync signal (timing.align, drift auto) then fits only the offset")
        self.clock_box.activated.connect(self._clock_chosen)
        layout.addWidget(self.clock_box)

        # ----------------------------------------------------------- latency
        layout.addWidget(heading("Latency"))
        self.latency_label = QLabel(content)
        self.latency_label.setTextFormat(Qt.RichText)
        self.latency_label.setWordWrap(True)
        layout.addWidget(self.latency_label)
        self.latency_box = QWidget(content)
        grid = QGridLayout(self.latency_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        self.loop_output = QComboBox(self.latency_box)
        self.loop_output.setToolTip("The output that is switched")
        self.loop_input = QComboBox(self.latency_box)
        self.loop_input.setToolTip("The channel the output is wired to")
        self.loop_rate = QComboBox(self.latency_box)
        self.loop_rate.setEditable(True)
        for text in ("10 kHz", "100 kHz", "1 MHz", "10 MHz"):
            self.loop_rate.addItem(text)
        self.loop_rate.setCurrentText("100 kHz")
        self.loop_rate.setToolTip("The latency is kept for this rate: measure at the rate you capture with")
        self.loop_mode = QComboBox(self.latency_box)
        self.loop_mode.addItem("Stream", "stream")
        self.loop_mode.addItem("Capture", "capture")
        self.loop_mode.currentIndexChanged.connect(self._fill_loop_rates)
        self.loop_input.currentIndexChanged.connect(self._fill_loop_rates)
        for column, (title, widget) in enumerate((("Output", self.loop_output), ("wired to", self.loop_input),
                                                   ("Rate", self.loop_rate), ("Mode", self.loop_mode))):
            grid.addWidget(QLabel(title, self.latency_box), 0, column)
            grid.addWidget(widget, 1, column)
        self.measure_button = QPushButton("Measure", self.latency_box)
        set_icon(self.measure_button, "ruler")
        set_variant(self.measure_button, "primary")
        self.measure_button.setToolTip("Switch the output ten times while the device streams the channel: half of "
                                       "the shortest loop is the latency (kept for the device at this rate)")
        self.measure_button.clicked.connect(self.measure_latency)
        grid.addWidget(self.measure_button, 1, 4)
        self.wire_button = QPushButton("Wire them (simulator)", self.latency_box)
        set_icon(self.wire_button, "plug")
        self.wire_button.setToolTip("Adds the wire from the output to the channel in the simulated circuit "
                                    "(Signals tab: Wires)")
        self.wire_button.clicked.connect(self.wire_loopback)
        grid.addWidget(self.wire_button, 1, 5)
        grid.setColumnStretch(6, 1)
        layout.addWidget(self.latency_box)
        self.latency_note = note("")
        layout.addWidget(self.latency_note)
        self.forget_latency_button = QPushButton("Forget the latencies", content)
        set_icon(self.forget_latency_button, "trash")
        self.forget_latency_button.clicked.connect(self.forget_latencies)
        row = QHBoxLayout()
        row.addWidget(self.forget_latency_button)
        self.calibrate_button = QPushButton("In a flow (example)...", content)
        set_icon(self.calibrate_button, "nodes")
        self.calibrate_button.setToolTip("Opens the example flow with the node timing.calibrate")
        self.calibrate_button.clicked.connect(lambda: self.example_requested.emit("13-time/02-calibrate-latency"))
        row.addWidget(self.calibrate_button)
        row.addStretch(1)
        layout.addLayout(row)

        # ------------------------------------------------------- sync output
        self.sync_heading = heading("Sync output")
        layout.addWidget(self.sync_heading)
        self.sync_box = QWidget(content)
        sync_row = QHBoxLayout(self.sync_box)
        sync_row.setContentsMargins(0, 0, 0, 0)
        sync_row.addWidget(QLabel("Pin", self.sync_box))
        self.sync_pin = QComboBox(self.sync_box)
        sync_row.addWidget(self.sync_pin)
        sync_row.addWidget(QLabel("Seed", self.sync_box))
        self.sync_seed = QSpinBox(self.sync_box)
        self.sync_seed.setRange(1, 999)
        self.sync_seed.setToolTip("The same seed, the same intervals (as timing.sync)")
        sync_row.addWidget(self.sync_seed)
        self.sync_button = QPushButton("Start", self.sync_box)
        set_icon(self.sync_button, "play")
        self.sync_button.clicked.connect(self.toggle_sync_output)
        sync_row.addWidget(self.sync_button)
        self.sync_state = QLabel(self.sync_box)
        set_role(self.sync_state, "hint")
        sync_row.addWidget(self.sync_state, 1)
        layout.addWidget(self.sync_box)
        self.sync_note = note("Edges at irregular intervals (20-60 ms) on the pin: wire it to a channel of every "
                              "device to align, e.g. an input of a DAQ that records it with its data. A flow "
                              "aligns by it with <i>timing.align</i> (or <i>remote.sync</i>).")
        layout.addWidget(self.sync_note)
        layout.addStretch(1)
        scroll.setWidget(content)
        self._fill_timing_choices()
        return scroll

    def _fill_timing_choices(self) -> None:
        """Outputs, channels and the stored choices of the Timing tab."""
        from ...core import timing_tools
        from ...core.instrument import PIN_DOUT

        pins = self.instrument.pins() if self.instrument.gpio is not None else []
        outputs = [pin.name for pin in pins if PIN_DOUT in pin.capabilities and pin.usable]
        channels = [pin.name for pin in self.instrument.pins() if pin.channel is not None]
        config = timing_tools.device_config(self.instrument)
        first = self.loop_output.count() == 0
        for box, names in ((self.loop_output, outputs), (self.loop_input, channels), (self.sync_pin, outputs)):
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            if current in names:
                box.setCurrentText(current)
            box.blockSignals(False)
        wired = self._simulated_loopback(outputs, channels)
        if wired is not None:
            if first:  # a wire the simulator already has (sim:daq: P0.0 → P0.1)
                self.loop_output.setCurrentText(wired[0])
                self.loop_input.setCurrentText(wired[1])
        elif outputs and self.loop_output.currentIndex() == 0 and channels:
            # an output that is not also the channel (GP16 → GP17)
            free = [name for name in outputs if name != channels[0]]
            if free and config["sync_pin"] != free[0]:
                self.loop_output.setCurrentText(free[0])
        self._fill_loop_rates()
        if config["sync_pin"] in outputs:
            self.sync_pin.setCurrentText(config["sync_pin"])
        self.sync_seed.setValue(int(config["sync_seed"] or 1))
        self.clock_box.setCurrentIndex(max(self.clock_box.findData(config["clock"]), 0))

    def _simulated_loopback(self, outputs: list[str], channels: list[str]) -> Optional[tuple[str, str]]:
        """The first wire of the simulated circuit from an output to a channel, ``None`` for none."""
        simulator = getattr(self.instrument, "simulated_driver", None)
        if simulator is None:
            return None
        for source, target in simulator.circuit.wires():
            if source in outputs and target in channels:
                return source, target
        return None

    def _fill_loop_rates(self) -> None:
        """The rates of the latency measurement: those the device reaches for the channel and mode."""
        from ...core.timing_tools import loopback_rate_limit

        if self.loop_input.count() == 0:
            return
        highest = loopback_rate_limit(self.instrument, self.loop_input.currentText(), self.loop_mode.currentData())
        if highest >= 1000:  # three digits, down: what is shown is reached (16.6 MHz, not 16.67)
            step = 10 ** (len(str(int(highest))) - 3)
            highest = highest // step * step
        rates = [10_000.0, 100_000.0, 1_000_000.0, 10_000_000.0]
        if highest:
            rates = [rate for rate in rates if rate <= highest]
            if not rates or rates[-1] < highest:
                rates.append(float(highest))
        try:
            current = float(units.parse(self.loop_rate.currentText(), "Hz"))
        except (TypeError, ValueError):
            current = 100_000.0
        if current <= 0 or (highest and current > highest):
            current = min(100_000.0, rates[-1])
        self.loop_rate.blockSignals(True)
        self.loop_rate.clear()
        self.loop_rate.addItems([units.format_quantity(rate, "Hz") for rate in rates])
        self.loop_rate.setCurrentText(units.format_quantity(current, "Hz"))
        self.loop_rate.blockSignals(False)

    def _clock_chosen(self) -> None:
        from ...core import timing_tools

        timing_tools.set_device_config(self.instrument, clock=self.clock_box.currentData())
        self._update_timing()

    def _update_timing(self) -> None:
        """The texts and buttons of the Timing tab."""
        if self.timing_page is None:
            return
        from ...core import timing, timing_tools

        instrument = self.instrument
        self.timing_label.setText(self._rows_html([row for row in self._timing_rows()
                                                   if row[0] != "Measured latencies"]))
        stored = timing.latencies_of(instrument)
        rows = [(title, (f"{units.format_quantity(latency.value, 's', 3)} ± "
                         f"{units.format_quantity(latency.uncertainty, 's', 3)} ({latency.source})"))
                for title, latency in stored.items()]
        self.latency_label.setText(self._rows_html(rows) if stored else (
            f"<span style='color:{TEXT_MUTED}'>None measured: captures are placed between their start and "
            "their arrival.</span>"))
        self.forget_latency_button.setVisible(bool(stored))
        connected = instrument in self.hub and instrument.status != InstrumentStatus.DISCONNECTED
        controller = self.controller
        capturing = controller is not None and controller.is_capturing
        has_gpio = instrument.gpio is not None and self.loop_output.count() > 0
        simulator = getattr(instrument, "simulated_driver", None)
        self.latency_box.setVisible(has_gpio)
        self.measure_button.setEnabled(connected and not capturing and self.loop_input.count() > 0)
        self.wire_button.setVisible(simulator is not None and self.signals_panel is not None)
        if not has_gpio:
            self.latency_note.setText("Measuring needs an output of the device wired to one of its channels; this "
                                      "device has no outputs. A latency of a flow (timing.calibrate with another "
                                      "instrument) or a sync signal places its samples instead.")
        elif simulator is not None and not getattr(simulator, "knows_time", True):
            self.latency_note.setText("The simulator emulates a USB link (Signals tab): wire the output to the "
                                      "channel there (or <i>Wire them</i>), then <i>Measure</i>.")
        elif simulator is not None:
            self.latency_note.setText("The simulator knows the time of its samples (exact): switch on its USB link "
                                      "in the Signals tab to measure as with the real device.")
        else:
            self.latency_note.setText("Wire the output to the channel (a jumper), then <i>Measure</i>. The latency "
                                      "is kept for this device, its mode and rate and used by its captures from "
                                      "then on.")
        output = timing_tools.sync_output_of(instrument)
        running = output is not None and output.running
        for widget in (self.sync_heading, self.sync_box, self.sync_note):
            widget.setVisible(instrument.gpio is not None and self.sync_pin.count() > 0)
        self.sync_button.setText("Stop" if running else "Start")
        set_icon(self.sync_button, "stop" if running else "play")
        self.sync_button.setEnabled(connected or running)
        self.sync_pin.setEnabled(not running)
        self.sync_seed.setEnabled(not running)
        if running:
            self.sync_state.setText(f"running on {output.pin}: {len(output.edges)} edges")
        elif output is not None and output.error:
            self.sync_state.setText(f"stopped: {output.error}")
        else:
            self.sync_state.setText("")

    def measure_latency(self) -> bool:
        """*Measure*: the loopback of the Timing tab, in the background (cancellable)."""
        import threading

        from ...core.timing_tools import measure_latency as measure
        from .. import background

        pin, channel = self.loop_output.currentText(), self.loop_input.currentText()
        if not pin or not channel:
            return False
        if pin == channel:
            self.show_banner("The output and the channel are the same pin: wire the output to another one.", "warning")
            return False
        try:
            rate = float(units.parse(self.loop_rate.currentText(), "Hz"))
        except (TypeError, ValueError):
            self.show_banner(f"{self.loop_rate.currentText()} is no rate (e.g. 100 kHz).", "warning")
            return False
        mode = self.loop_mode.currentData()
        stop = threading.Event()
        try:
            latency = background.run(self, f"Measuring the latency: {pin} → {channel}...",
                                     lambda: measure(self.instrument, pin, channel, rate=rate, mode=mode,
                                                     cancelled=stop.is_set))
        except background.Cancelled:
            stop.set()
            return False
        except Exception as error:  # noqa: BLE001 - every way a measurement fails is shown (LatencyError, ...)
            self.show_banner(f"The latency could not be measured: {error}", "error")
            self._update_timing()
            return False
        self.show_banner(f"Latency of {self.instrument.name} ({mode} at {units.format_quantity(rate, 'Hz')}): "
                         f"{units.format_quantity(latency.value, 's', 3)} ± "
                         f"{units.format_quantity(latency.uncertainty, 's', 3)} - kept for its captures.",
                         "info")
        self._update_timing()
        return True

    def wire_loopback(self) -> bool:
        """*Wire them*: the wire from the output to the channel in the simulated circuit."""
        if self.signals_panel is None:
            return False
        pin, channel = self.loop_output.currentText(), self.loop_input.currentText()
        if not pin or not channel or pin == channel:
            return False
        simulator = getattr(self.instrument, "simulated_driver", None)
        if simulator is not None and (pin, channel) in simulator.circuit.wires():
            self.show_banner(f"{pin} is already wired to {channel} in the simulated circuit.", "info")
            return True
        done = self.signals_panel.add_wire(pin, channel)
        if done:
            self.show_banner(f"{pin} is wired to {channel} in the simulated circuit (Signals tab: Wires).", "info")
        else:
            self.show_banner(f"{pin} cannot be wired to {channel}: {self.signals_panel.wire_error}", "error")
        self._update_timing()
        return done

    def toggle_sync_output(self) -> bool:
        """Start or stop the sync signal on the chosen pin (kept for the device)."""
        from ...core import timing_tools

        if timing_tools.sync_output_of(self.instrument) is not None and \
                timing_tools.sync_output_of(self.instrument).running:
            timing_tools.stop_sync_output(self.instrument)
            self._update_timing()
            return False
        pin, seed = self.sync_pin.currentText(), self.sync_seed.value()
        if not pin:
            return False
        timing_tools.set_device_config(self.instrument, sync_pin=pin, sync_seed=seed)
        try:
            timing_tools.start_sync_output(self.instrument, pin, seed)
        except InstrumentError as error:
            self.show_banner(f"The sync signal could not be started on {pin}: {error}", "error")
            return False
        self.action_performed.emit(self.instrument, f"sync signal on {pin}")
        self._update_timing()
        return True

    def _update_shown_details(self) -> None:
        if not self.isVisible():
            return
        if self.tabs.currentWidget() is self.details_page:
            self._update_details()
        elif self.timing_page is not None and self.tabs.currentWidget() is self.timing_page:
            self._update_timing()

    def forget_latencies(self) -> int:
        """Forget the latencies measured for this device (the next captures use start and arrival)."""
        from ...core import timing

        count = timing.forget_latencies(self.instrument)
        self._update_timing()
        return count

    @staticmethod
    def _rows_html(rows: list[tuple[str, str]]) -> str:
        body = "".join(
            f"<tr><td style='color:{TEXT_MUTED}; padding-right:12px'>{html.escape(str(name))}</td>"
            f"<td>{html.escape(str(value))}</td></tr>" for name, value in rows)
        return f"<table cellspacing='0' cellpadding='1'>{body}</table>"

    def _update_details(self) -> None:
        """The texts of the Details tab (see :meth:`_detail_sections`)."""
        instrument = self.instrument
        limits = self._limit_rows()
        self.limits_heading.setVisible(bool(limits))
        self.limits_table.setVisible(bool(limits))
        if limits and self.limits_table.rowCount() != len(limits):
            self.limits_table.setRowCount(len(limits))
            for row, values in enumerate(limits):
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.limits_table.setItem(row, column, item)
            self.limits_table.resizeRowsToContents()
            height = self.limits_table.horizontalHeader().height() + 4 + sum(
                self.limits_table.rowHeight(row) for row in range(len(limits)))
            self.limits_table.setFixedHeight(height)
        capabilities = sorted(instrument.capabilities())
        facets = [facet.title for facet in instrument.facets()]
        unlocked = sorted(facets_of(capabilities))
        parts = [f"<b>Facets:</b> {html.escape(', '.join(facets) or 'none')}"]
        parts.append("<b>Reported:</b> " + (html.escape(", ".join(capabilities)) if capabilities
                                           else f"<span style='color:{TEXT_MUTED}'>none</span>"))
        if unlocked:
            parts.append(f"<b>Unlocks:</b> {html.escape(', '.join(unlocked))}")
        self.capabilities_label.setText("<br>".join(parts))
        sections = [f"<b>{html.escape(title)}</b>{self._rows_html(rows)}"
                    for title, rows in instrument.details() if title != "Timing"]
        self.details_label.setText("<br>".join(sections))

    @property
    def title(self) -> str:
        return self.instrument.name

    # ------------------------------------------------------------ refresh
    def _name_channels(self, names: dict) -> None:
        """The names the simulated signals suggest, for the next capture of the device."""
        if self.controller is not None:
            self.controller.use_channel_names(names)

    def _signals_applied(self, config: dict, _names: dict) -> None:
        """The simulator simulates something else: remember it for this device, show it."""
        from ..devices.simulated import remember_signals

        if self.instrument.uri:
            remember_signals(self.instrument.uri, config)
        self.refresh()

    def _address_changed(self, address: str) -> None:
        """A simulated device runs with other settings: its address says them (reconnect, session)."""
        self.instrument.uri = address
        self.refresh()

    def _restart_at(self, address: str) -> None:
        """Open the device again at ``address`` (a simulated remote device with another clock)."""
        self.instrument.uri = address
        self.reconnect_requested.emit(self.instrument)

    def _circuit_changed(self, values: dict) -> None:
        """Wires or the USB link of the simulator changed: remember them for this device."""
        from ..devices.simulated import remember_circuit

        if self.instrument.uri:
            remember_circuit(self.instrument.uri, **values)
        self.refresh()

    def show_banner(self, text: str, kind: str = "error") -> None:
        """A message about the device above the tabs (``kind``: info, warning, error)."""
        from ..theme import set_role

        set_role(self.banner, f"banner-{kind}")
        self.banner.set_message(html.escape(text), "Dismiss" if text else None)

    def refresh(self) -> None:
        instrument = self.instrument
        self.name_label.setText(instrument.name)
        self.name_label.setToolTip(instrument.name)
        color = token(instrument.status.token)
        self.status_label.setText(f"<span style='color:{color}'>● {instrument.status.value}</span>")
        driver = instrument.capture.driver if instrument.capture is not None else None
        subtitle = [instrument.kind]
        if instrument.uri:
            subtitle.append(instrument.uri)
        if driver is not None and driver.is_hardware and driver.device_version and \
                firmware.parse_device_version(driver.device_version) is not None:
            subtitle.append(firmware.describe_firmware(driver.device_version))
        self.kind_label.setText("  ·  ".join(subtitle))
        self.data_button.setVisible(instrument.capture is not None)
        self.action_firmware.setVisible(driver is not None and firmware.update_method(driver) is not None)
        connected = instrument in self.hub and instrument.status != InstrumentStatus.DISCONNECTED
        for widget in (self.data_button, self.disconnect_button, self.safe_button, self.record_button):
            widget.setEnabled(connected)
        self.action_firmware.setEnabled(connected)
        failed = connected and instrument.status == InstrumentStatus.ERROR
        self.disconnect_button.setVisible(connected)
        self.reconnect_button.setVisible((not connected or failed) and bool(instrument.uri))
        if not connected:
            reason = "was unplugged" if instrument.status == InstrumentStatus.DISCONNECTED else "is disconnected"
            self.show_banner(f"{instrument.name} {reason}. Reconnect it to use it again.", "warning")
        elif failed:
            self.show_banner(f"{instrument.name} does not answer. Check the connection, then Reconnect.", "error")
        elif self.banner.isVisible() and "Reconnect" in self.banner.label.text():
            self.banner.setVisible(False)
        self._update_capture()
        has_monitor = instrument.monitor is not None
        self.monitor_box.setEnabled(connected and has_monitor)
        for widget in (self.record_button, self.monitor_box, self.monitor_rate):
            widget.setVisible(has_monitor)

        self._update_details()
        if self.timing_page is not None:
            self._fill_timing_choices()
            self._update_timing()

        pins = instrument.pins()
        signature = (instrument.gpio is not None, tuple(
            (pin.name, pin.channel, pin.analog_channel, tuple(sorted(pin.capabilities)), pin.usable) for pin in pins))
        if signature != self._pin_signature:
            self._pin_signature = signature
            self._fill_pins(pins)  # switches, pulse and PWM values stay as they are otherwise
        else:
            self._update_pin_rows(pins)

        self.document_changed.emit()

    def _fill_pins(self, pins: list[PinInfo]) -> None:
        """One row per pin: what it is (channel, name, capabilities) and, with GPIO, what it does."""
        gpio = self.instrument.gpio
        has_gpio = gpio is not None
        self.strip.setVisible(has_gpio)
        self.safe_button.setVisible(has_gpio)
        self.stimulus_row.setVisible(has_gpio)
        for column in (COL_MODE, COL_VALUE, COL_ACTION):
            self.pin_table.setColumnHidden(column, not has_gpio)
        if has_gpio:
            self.strip.set_pins(pins)
            self.strip.modes = {pin.name: gpio.mode(pin.name) for pin in pins}
            self._fill_stimulus_targets()
        self.pin_table.setRowCount(len(pins))
        self.value_labels.clear()
        self.history.clear()
        for row, pin in enumerate(pins):
            channel = "–" if pin.channel is None else str(pin.channel)
            if pin.analog_channel is not None:
                channel = f"{channel} / A{pin.analog_channel}" if pin.channel is not None else f"A{pin.analog_channel}"
            can = ", ".join(sorted(pin.capabilities)) + f" · {pin.logic_level:g} V"
            if not pin.usable:
                can = f"reserved: {pin.reserved}"
            for column, value in ((COL_PIN, pin.name), (COL_CHANNEL, channel), (COL_NAME, self.channel_name(pin)),
                                  (COL_CAN, can)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if not pin.usable:
                    item.setForeground(QColor(TEXT_MUTED))
                    item.setToolTip(f"Reserved: {pin.reserved}")
                self.pin_table.setItem(row, column, item)
            if not has_gpio:
                continue
            mode = QComboBox(self.pin_table)
            for item in modes_of(pin):
                mode.addItem(item, item)
            mode.setCurrentIndex(max(mode.findData(gpio.mode(pin.name)), 0))
            mode.setEnabled(pin.usable)
            mode.currentIndexChanged.connect(lambda _index, name=pin.name, box=mode: self.set_mode(name, box.currentData()))
            self.pin_table.setCellWidget(row, COL_MODE, mode)
            value = QWidget(self.pin_table)
            value_layout = QHBoxLayout(value)
            value_layout.setContentsMargins(4, 0, 4, 0)
            label = QLabel("–", value)
            value_layout.addWidget(label)
            self.value_labels[pin.name] = label
            if PIN_ADC in pin.capabilities:
                line = Sparkline(value)
                value_layout.addWidget(line)
                self.history[pin.name] = line
            self.pin_table.setCellWidget(row, COL_VALUE, value)
            self.pin_table.setCellWidget(row, COL_ACTION, self._actions(pin))
        header = self.pin_table.horizontalHeader()
        for column, width in PIN_WIDTHS.items():
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.pin_table.setColumnWidth(column, width)
        if has_gpio:
            # the actions keep their size; what a pin can do is cut (its tooltip has it all)
            header.setSectionResizeMode(COL_ACTION, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(COL_CAN, QHeaderView.Stretch)
        else:
            header.setSectionResizeMode(COL_CAN, QHeaderView.Stretch)
        self.pin_table.resizeRowsToContents()

    def _update_pin_rows(self, pins: list[PinInfo]) -> None:
        """After a status change: what can change without other pins (enabled, the mode)."""
        gpio = self.instrument.gpio
        connected = self.instrument in self.hub
        for row, pin in enumerate(pins):
            for column in (COL_MODE, COL_ACTION):
                widget = self.pin_table.cellWidget(row, column)
                if widget is not None:
                    widget.setEnabled(connected and pin.usable)
            mode = self.pin_table.cellWidget(row, COL_MODE)
            if gpio is not None and isinstance(mode, QComboBox):
                index = mode.findData(gpio.mode(pin.name))
                if index >= 0 and index != mode.currentIndex():
                    mode.blockSignals(True)
                    mode.setCurrentIndex(index)
                    mode.blockSignals(False)
        self._update_names()

    def _update_names(self) -> None:
        """The channel names of the capture settings in the pin table (after *Apply*)."""
        if not hasattr(self, "pin_table"):
            return
        pins = {pin.name: pin for pin in self.instrument.pins()}
        for row in range(self.pin_table.rowCount()):
            item = self.pin_table.item(row, COL_PIN)
            pin = pins.get(item.text()) if item is not None else None
            if pin is not None:
                name = self.pin_table.item(row, COL_NAME)
                if name is not None:
                    name.setText(self.channel_name(pin))

    def _actions(self, pin: PinInfo) -> QWidget:
        """The actions a pin offers, one line each: switch and pulse, PWM, DAC."""
        widget = QWidget(self.pin_table)
        layout = QGridLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setHorizontalSpacing(4)
        layout.setVerticalSpacing(2)
        if not pin.usable:
            layout.addWidget(QLabel("–", widget), 0, 0)
            return widget
        line = 0
        if PIN_DOUT in pin.capabilities:
            switch = QPushButton("Off", widget)
            switch.setCheckable(True)
            switch.setObjectName(f"switch-{pin.name}")
            switch.setToolTip(f"Drive {pin.name} high or low")
            switch.toggled.connect(lambda on, name=pin.name, button=switch: self._switch(name, button, on))
            layout.addWidget(switch, line, 0)
            width = QDoubleSpinBox(widget)
            width.setDecimals(2)
            width.setRange(0.01, 10_000)
            width.setValue(10)
            width.setSuffix(" ms")
            width.setToolTip("Width of the pulse")
            width.setObjectName(f"width-{pin.name}")
            layout.addWidget(width, line, 1)
            pulse = QPushButton("Pulse", widget)
            pulse.clicked.connect(lambda _checked=False, name=pin.name, box=width: self.pulse(name, box.value() / 1000))
            layout.addWidget(pulse, line, 3)
            line += 1
        if PIN_PWM in pin.capabilities:
            frequency = QDoubleSpinBox(widget)
            frequency.setRange(1, 1_000_000)
            frequency.setDecimals(0)
            frequency.setValue(1000)
            frequency.setSuffix(" Hz")
            frequency.setToolTip("PWM frequency")
            frequency.setObjectName(f"frequency-{pin.name}")
            layout.addWidget(frequency, line, 1)
            duty = QSpinBox(widget)
            duty.setRange(0, 100)
            duty.setSuffix(" %")
            duty.setToolTip("PWM duty cycle")
            duty.setObjectName(f"duty-{pin.name}")
            layout.addWidget(duty, line, 2)
            apply = QPushButton("PWM", widget)
            apply.setToolTip(f"Start PWM on {pin.name}")
            apply.clicked.connect(lambda _checked=False, name=pin.name, f=frequency, d=duty: self.pwm(
                name, f.value(), d.value() / 100))
            layout.addWidget(apply, line, 3)
            line += 1
        if PIN_DAC in pin.capabilities and self.instrument.facet(_analog_out()) is not None:
            volts = QDoubleSpinBox(widget)
            volts.setRange(-10, 10)
            volts.setDecimals(3)
            volts.setSuffix(" V")
            volts.setToolTip("Output voltage")
            layout.addWidget(volts, line, 1)
            dac = QPushButton("Set", widget)
            dac.clicked.connect(lambda _checked=False, name=pin.name, box=volts: self.set_voltage(name, box.value()))
            layout.addWidget(dac, line, 3)
        layout.setColumnStretch(4, 1)
        return widget

    def _switch(self, name: str, button: QPushButton, on: bool) -> None:
        """Drive ``name``; the switch goes back when the pin was not driven (declined, error)."""
        if self.write(name, 1 if on else 0) is False:
            button.blockSignals(True)
            button.setChecked(not on)
            button.blockSignals(False)
            on = not on
        button.setText("On" if on else "Off")

    # ------------------------------------------------------------ actions
    def _confirm_output(self, pin: str) -> bool:
        """The first time a pin drives: warn about the level (5 V into a 3.3 V input, short circuits)."""
        if pin in self._confirmed:
            return True
        info = self.instrument.gpio.pin(pin)
        if not messages.confirm(self, "Output", f"{pin} will drive {info.logic_level:g} V.",
                                "Drive it", "Check what is connected: a 5 V output can damage a 3.3 V input, "
                                "and two outputs against each other short-circuit."):
            return False
        self._confirmed.add(pin)
        return True

    def _do(self, text: str, action) -> bool:
        try:
            action()
        except InstrumentError as error:
            messages.warning(self, "Device", str(error))
            return False
        self.action_performed.emit(self.instrument, text)
        self._refresh_modes()
        return True

    def set_mode(self, pin: str, mode: str) -> bool:
        if mode in (MODE_OUTPUT, MODE_PWM) and not self._confirm_output(pin):
            self._refresh_modes()
            return False
        return self._do(f"{pin}: {mode}", lambda: self.instrument.gpio.set_mode(pin, mode))

    def write(self, pin: str, value: int) -> bool:
        if not self._confirm_output(pin):
            return False
        return self._do(f"{pin} = {value}", lambda: self.instrument.gpio.write(pin, value))

    def pulse(self, pin: str, width: float) -> bool:
        if not self._confirm_output(pin):
            return False
        return self._do(f"{pin}: pulse {units.format_quantity(width, 's', 3)}",
                        lambda: self.instrument.gpio.pulse(pin, width))

    def pwm(self, pin: str, frequency: float, duty: float) -> bool:
        if not self._confirm_output(pin):
            return False
        return self._do(f"{pin}: PWM {units.format_quantity(frequency, 'Hz', 3)} {duty * 100:g} %",
                        lambda: self.instrument.gpio.pwm(pin, frequency, duty))

    def set_voltage(self, pin: str, volts: float) -> bool:
        facet = self.instrument.facet(_analog_out())
        return self._do(f"{pin} = {volts:g} V", lambda: facet.set_voltage(pin, volts))

    def all_safe(self) -> None:
        gpio = self.instrument.gpio
        if gpio is None:
            return
        self._do("all outputs safe", gpio.safe_all)
        for row in range(self.pin_table.rowCount()):
            actions = self.pin_table.cellWidget(row, COL_ACTION)
            for button in actions.findChildren(QPushButton) if actions else []:
                if button.isCheckable() and button.isChecked():
                    button.blockSignals(True)
                    button.setChecked(False)
                    button.setText("Off")
                    button.blockSignals(False)

    def _refresh_modes(self) -> None:
        gpio = self.instrument.gpio
        if gpio is None:
            return
        for row in range(self.pin_table.rowCount()):
            name = self.pin_table.item(row, COL_PIN).text()
            box = self.pin_table.cellWidget(row, COL_MODE)
            if isinstance(box, QComboBox):
                box.blockSignals(True)
                box.setCurrentIndex(max(box.findData(gpio.mode(name)), 0))
                box.blockSignals(False)
        self.strip.modes = {pin.name: gpio.mode(pin.name) for pin in self.strip.pins}
        self.strip.update()

    def _fill_stimulus_targets(self) -> None:
        current = self.stimulus_target.currentText()
        self.stimulus_target.clear()
        for instrument in self.hub.instruments():
            if instrument.capture is not None:
                self.stimulus_target.addItem(instrument.name, instrument)
        self.stimulus_target.setCurrentIndex(max(self.stimulus_target.findText(current), 0))
        self.stimulus_button.setEnabled(self.stimulus_target.count() > 0 and self.instrument in self.hub)

    def selected_pin(self) -> Optional[str]:
        row = self.pin_table.currentRow()
        return None if row < 0 else self.pin_table.item(row, COL_PIN).text()

    def _request_stimulus(self) -> None:
        pin = self.selected_pin()
        capture = self.stimulus_target.currentData()
        if pin is None or capture is None:
            messages.info(self, "Stimulus and capture", "Select an output pin and an instrument that captures.")
            return
        width = self.pin_table.cellWidget(self.pin_table.currentRow(), COL_ACTION).findChild(QDoubleSpinBox, f"width-{pin}")
        if width is None:
            messages.info(self, "Stimulus and capture", f"{pin} cannot pulse.")
            return
        if self._confirm_output(pin):
            self.stimulus_requested.emit(capture, self.instrument, pin, width.value() / 1000)

    def _select_pin(self, name: str) -> None:
        for row in range(self.pin_table.rowCount()):
            if self.pin_table.item(row, COL_PIN).text() == name:
                self.pin_table.selectRow(row)

    # ------------------------------------------------------------ monitor
    def sync_monitor(self) -> None:
        """The *Monitor* box shows whether the monitor runs (a recording starts and stops it too);
        while it runs the card listens to it."""
        monitor = self.instrument.monitor
        try:
            running = bool(monitor is not None and self.instrument.status != InstrumentStatus.DISCONNECTED
                           and monitor.running)
        except (DeviceConnectionError, InstrumentError):  # (it went away between two ticks of the timer)
            log.debug("The monitor of %s does not answer: not running", self.instrument.name, exc_info=True)
            running = False
        if running and self._remove_handler is None:
            self._remove_handler = monitor.on_state(self._monitor_state.emit)
        elif not running and self._remove_handler is not None:
            remove, self._remove_handler = self._remove_handler, None
            try:
                remove()
            except (DeviceConnectionError, InstrumentError):  # (nothing left to listen to)
                log.debug("The monitor handler of %s cannot be removed", self.instrument.name, exc_info=True)
        if not running:
            self._monitor_owned = False
        if self.monitor_box.isChecked() != running:
            self.monitor_box.blockSignals(True)
            self.monitor_box.setChecked(running)
            self.monitor_box.blockSignals(False)

    def set_monitoring(self, enabled: bool) -> None:
        monitor = self.instrument.monitor
        if monitor is None:
            return
        if self._remove_handler is not None:
            self._remove_handler()
            self._remove_handler = None
        self._monitor_owned = enabled
        if not enabled:
            monitor.stop()
            return
        pins = [pin for pin in self.instrument.pins() if pin.usable or pin.channel is not None]
        digital = [pin.name for pin in pins if "DIN" in pin.capabilities]
        analog = tuple(pin.name for pin in pins if PIN_ADC in pin.capabilities)
        self._remove_handler = monitor.on_state(self._monitor_state.emit)
        try:
            monitor.start(self.monitor_rate.value(), digital, analog)
        except InstrumentError as error:
            messages.warning(self, "Monitor", str(error))
            self.monitor_box.setChecked(False)

    def _queue_state(self, state: MonitorState) -> None:
        first = self._pending_state is None
        self._pending_state = state
        if first and not self._state_timer.isActive():
            self._state_timer.start()

    def _show_pending_state(self) -> None:
        state, self._pending_state = self._pending_state, None
        if state is not None:
            self.show_state(state)

    def show_state(self, state: MonitorState) -> None:
        for pin, level in state.digital.items():
            label = self.value_labels.get(pin)
            if label is not None and pin not in state.analog:
                label.setText("● 1" if level else "○ 0")
        for pin, volts in state.analog.items():
            label = self.value_labels.get(pin)
            if label is not None:
                label.setText(units.format_quantity(volts, "V", 3))
            line = self.history.get(pin)
            if line is not None:
                line.add(volts)
        self.strip.levels = dict(state.digital)
        self.strip.update()

    # -------------------------------------------------------------- shell
    def _on_hub_event(self, event) -> None:
        if self.instrument.gpio is not None:
            self._fill_stimulus_targets()
        if event.name != self.instrument.name:
            return
        if event.kind in (EVENT_STATUS, EVENT_REMOVED):
            self.refresh()

    def inspector_widget(self, selection=None) -> Optional[QWidget]:
        if getattr(self, "_inspector", None) is None:
            self._inspector = QLabel(self)
            self._inspector.setTextFormat(Qt.RichText)
            self._inspector.setWordWrap(True)
            self._inspector.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self._inspector.setContentsMargins(10, 6, 10, 6)
            self._inspector.hide()
        instrument = self.instrument
        self._inspector.setText(
            f"<b>{html.escape(instrument.name)}</b><br>{html.escape(instrument.kind)}<br>"
            f"{html.escape(instrument.uri)}<br>{len(instrument.pins())} pins"
        )
        return self._inspector

    def shutdown(self) -> None:
        from ...core import timing_tools

        timing_tools.stop_sync_output(self.instrument)  # (a sync signal runs while its card is open)
        self.monitor_timer.stop()
        self._details_timer.stop()
        if self.controller is not None:
            self.controller.power_timer.stop()
            try:
                self.controller.changed.disconnect(self._update_capture)
            except (RuntimeError, TypeError):
                pass
        # only the monitor this card started: one a recording runs keeps running
        if self.instrument.monitor is not None and self._monitor_owned:
            self.instrument.monitor.stop()
        if self._remove_handler is not None:
            self._remove_handler()
        model = getattr(self.instrument, "simulated_driver", None)
        if model is not None and self._device_events is not None:
            model.remove_event_listener(self._device_events)
        self.bridge.close()


def _analog_out():
    from ...core.instrument import AnalogOutFacet

    return AnalogOutFacet

