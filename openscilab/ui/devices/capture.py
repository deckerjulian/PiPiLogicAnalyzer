# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Capturing with an instrument: what the device card offers, the data view shows the result.

One :class:`CaptureController` per instrument with a capture facet (:func:`capture_controller`).
It starts captures (settings dialog, the quick settings, repeat, stimulus and capture), keeps the
profiles of capture settings, runs the device functions (information, self-test, network,
bootloader, generated captures) and forwards what the driver reports – progress, tiles,
completion – to the data view it captures into (:attr:`view`), which displays it.
"""

from __future__ import annotations

import threading

from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMenu, QWidget

from ...core import capture_io, settings
from ...core.instrument import Instrument
from ...core.profiles import (
    PROFILE_FILE_FILTER,
    Profile,
    ProfileStore,
    fit_session,
    profile_problem,
    profiles_directory,
    read_profiles_file,
    write_profiles_file,
)
from ...driver.base import CAPABILITY_SIMULATION, AnalyzerDriverBase, CaptureError
from ...driver.models import CaptureSession
from .. import background, messages
from ..icons import icon

#: Stimulus and capture: the action waits this long after the capture started (the device arms)
ARM_DELAY_MS = 50
POWER_POLL_INTERVAL_MS = 30_000


class CaptureBridge(QObject):
    """Marshals capture completion and progress from the driver thread to the UI thread."""

    completed = Signal(object)
    progress = Signal(object)
    #: parts of a progressively transferred capture arrived
    tile = Signal(object)


#: words of errors that mean the device is gone (not that the capture went wrong)
_CONNECTION_WORDS = ("connection closed", "not connected", "disconnected", "device not configured", "no such device",
                     "broken pipe", "connection reset", "timeout waiting for", "port is closed", "no device",
                     "errno 6]", "errno 19]", "errno 32]", "errno 54]", "errno 60]", "libusb", "no_device")


def connection_error(text: str) -> bool:
    """Whether the error text of a failed capture says the device is gone."""
    lowered = text.lower()
    return any(word in lowered for word in _CONNECTION_WORDS)


#: after a stop: how often and how long the controller waits for the driver to be idle
ABORT_CHECK_MS = 50
ABORT_CHECKS = 100


class CaptureController(QObject):
    """Captures with one instrument into a data view (see the module documentation)."""

    #: a capture started or ended, the view or the power status changed: the device card updates
    changed = Signal()
    #: a profile stored new capture settings (the capture bar reads them again)
    profile_loaded = Signal()
    #: the state of the running capture changed (armed, receiving n %, ended, failed)
    state_changed = Signal()
    #: the device stopped answering (a failed capture, no answer to the power question): why
    connection_lost = Signal(str)
    _power_answer = Signal(object)

    def __init__(self, instrument: Instrument, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.instrument = instrument
        self.driver: AnalyzerDriverBase = instrument.capture.driver
        self.profiles = ProfileStore()
        #: the data view the captures go to (it registers itself, see DataView.use_instrument)
        self.view = None
        #: the decoders of a profile loaded while no data view captures for the device: the next one
        #: gets them (``DataView.attach_source``); loading a profile opens no view
        self.pending_decoders: Optional[list] = None
        #: finds or opens a data view for this instrument (set by the shell)
        self.view_provider: Optional[Callable[["CaptureController"], object]] = None
        #: the settings of the toolbar of the device card (QuickCaptureBar), when it has one
        self.quick_settings: Optional[Callable[[], Optional[CaptureSession]]] = None
        #: shows a data view without activating it (set by the shell)
        self.reveal_view: Optional[Callable] = None
        self.last_session: Optional[CaptureSession] = None
        #: "<volts> V (<source>)" of a network board, polled while the device card is open
        self.power_text = ""
        self.bridge = CaptureBridge(self)
        self.bridge.completed.connect(self._completed)
        self.bridge.progress.connect(self._progress)
        #: share of the samples received in the running capture (``None``: armed, nothing yet)
        self.received: Optional[float] = None
        #: why the last capture failed ("" when it did not)
        self.error = ""
        #: the time of the samples of the running capture (``core.timing.Acquisition``)
        self.acquisition = None
        self._running = False
        self.bridge.tile.connect(lambda args: self.view is not None and self.view._on_tile(args))
        self.driver.add_capture_completed_handler(self.bridge.completed.emit)
        self.driver.add_capture_progress_handler(self.bridge.progress.emit)
        self.driver.add_capture_tile_handler(self.bridge.tile.emit)
        self._power_busy = False
        self._power_answer.connect(self._show_power)
        self._abort_timer = QTimer(self)
        self._abort_timer.setInterval(ABORT_CHECK_MS)
        self._abort_timer.timeout.connect(self._check_abort)
        self._abort_checks = 0
        self.power_timer = QTimer(self)
        self.power_timer.setInterval(POWER_POLL_INTERVAL_MS)
        self.power_timer.timeout.connect(self.update_power_status)

    # ------------------------------------------------------------ state
    @property
    def name(self) -> str:
        return self.instrument.name

    @property
    def is_capturing(self) -> bool:
        return self.driver.is_capturing

    @property
    def is_hardware(self) -> bool:
        return self.driver.is_hardware

    def can_simulate_on_board(self) -> bool:
        if self.driver.board_count != 1:
            return False
        try:
            return CAPABILITY_SIMULATION in self.driver.capabilities()
        except Exception:  # noqa: BLE001 - a device that cannot tell
            return False

    def close(self) -> None:
        """The instrument left the hub: stop listening to its driver."""
        self.power_timer.stop()
        self._abort_timer.stop()
        self.quick_settings = None  # the bar of a card that may be closed
        if getattr(self.instrument, "capture_controller", None) is self:
            del self.instrument.capture_controller  # a reconnected device gets a new one
        self.driver.remove_capture_completed_handler(self.bridge.completed.emit)
        self.driver.remove_capture_progress_handler(self.bridge.progress.emit)
        self.driver.remove_capture_tile_handler(self.bridge.tile.emit)
        if self.view is not None:
            self.view.detach_source(self)
        self.view = None

    # ------------------------------------------------------------ views
    def ensure_view(self):
        """The data view to capture into (one is opened when there is none)."""
        if self.view is None and self.view_provider is not None:
            self.view_provider(self)
        return self.view

    def settings_dialog(self, parent: Optional[QWidget] = None, accept_text: Optional[str] = None):
        from ..dialogs.capture_dialog import CaptureDialog

        view = self.view
        decoders = view.provider.to_list() if view is not None else []
        return CaptureDialog(self.driver, parent, profiles=self.profiles, decoder_configuration=decoders,
                             accept_text=accept_text, allow_apply=True)

    # ---------------------------------------------------------- capture
    def settings(self) -> Optional[CaptureSession]:
        """The settings of the next capture: the capture bar of the data view (which shows what
        *Apply* stored), else the stored settings, else those of the last capture, else the default."""
        session = self.quick_settings() if self.quick_settings is not None else None
        if session or self.stored_settings() or self.last_session:
            return session or self.stored_settings() or self.last_session
        from ..widgets.quick_capture import next_session

        return next_session(self.driver)  # (no data view yet: what its capture bar will start with)

    def channel_names(self) -> dict[int, str]:
        """Names of the channels in the settings: channel number -> name (digital channels)."""
        session = self.settings()
        if session is None:
            return {}
        return {channel.channel_number: channel.channel_name for channel in session.capture_channels
                if channel.channel_name}

    def analog_names(self) -> dict[int, str]:
        session = self.settings()
        if session is None:
            return {}
        return {channel.channel_number: channel.channel_name for channel in session.analog_channels
                if channel.channel_name}

    def start_capture(self, parent: Optional[QWidget] = None) -> bool:
        """The settings dialog: *Start capture* captures, *Apply* only keeps the settings."""
        if self.is_capturing:
            return False
        dialog = self.settings_dialog(parent)
        if not dialog.exec() or dialog.selected_settings is None:
            return False
        if dialog.applied_only:
            self.last_session = dialog.selected_settings
            self.changed.emit()
            return False
        return self.capture(dialog.selected_settings)

    def quick_start_capture(self, parent: Optional[QWidget] = None) -> bool:
        """Capture with the settings of the device card's toolbar (the stored settings)."""
        if self.is_capturing:
            return False
        session = self.quick_settings() if self.quick_settings is not None else None
        if session is None:
            session = self.stored_settings()
        if session is None:
            return self.start_capture(parent)
        return self.capture(session)

    def repeat_capture(self) -> bool:
        """Capture again with the settings of the last capture (or the one the view shows); the
        settings as captured, not the samples edited since (deleted or added channels)."""
        if self.is_capturing:
            return False
        session = self.last_session
        if session is None and self.view is not None and self.view.model.session is not None \
                and self.view.source is self:
            session = self.view.model.session
        if session is None:
            messages.warning(self.view, "Repeat capture", "There is no capture to repeat.",
                             f"Start a capture with {self.name} first.")
            return False
        return self.capture(session.clone_settings())

    def use_channel_names(self, names: dict[int, str]) -> bool:
        """The next capture of this device names its channels ``names`` (channel number -> name)."""
        from ..dialogs.capture_dialog import capture_settings_file

        session = self.settings()
        if session is None:
            return False
        session = session.clone_settings()
        for channel in session.capture_channels:
            if channel.channel_number in names:
                channel.channel_name = names[channel.channel_number]
        settings.persist_settings(capture_settings_file(self.driver),
                                  capture_io.session_to_dict(session, include_samples=False))
        self.profile_loaded.emit()  # the capture bar reads the settings again
        self.changed.emit()
        return True

    def stored_settings(self) -> Optional[CaptureSession]:
        """The settings last used with this kind of device (the capture dialog stores them)."""
        from ..dialogs.capture_dialog import capture_settings_file

        data = settings.get_settings(capture_settings_file(self.driver))
        if not data:
            return None
        try:
            return capture_io.session_from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def capture(self, session: CaptureSession) -> bool:
        """Start ``session``; its progress and result go to the data view (another one when the
        user changed the data of the view and did not save them)."""
        if self.is_capturing:
            return False
        if self.view is not None and getattr(self.view, "has_edits", False):
            self.view.detach_source(self)
        view = self.ensure_view()
        from ...core import timing_tools

        # the time of its samples, as the flows keep it (the Timing tab of the device card shows it)
        self.acquisition = timing_tools.begin(self.instrument, session)
        error = self.driver.start_capture(session)
        if error != CaptureError.NONE:
            self.acquisition = None
            messages.error(view, "Capture", "The capture could not be started.", error.message)
            return False
        timing_tools.started(self.acquisition, self.driver)
        self.last_session = session
        self._running = True
        self.received = None
        self.error = ""
        if view is not None:
            if self.reveal_view is not None:
                self.reveal_view(view)  # in front in its group, the focus stays where it is
            view.capture_started(session)
        self.changed.emit()
        self.state_changed.emit()
        return True

    def abort(self) -> None:
        """Stop the running capture. A stopped stream completes with what it has; a stopped
        buffer capture reports nothing (as the devices do), so the controller ends it itself."""
        if self.is_capturing:
            # stopping a stream waits for its end and may reconnect: not in the window's thread
            try:
                background.run(self.view, "Stopping the capture...", self.driver.stop_capture, cancellable=False)
            except Exception as error:  # noqa: BLE001 - a device that is gone cannot be stopped
                self.connection_lost.emit(str(error))
            if self.view is not None:
                self.view.statusBar().showMessage("Capture aborted", 5000)
            self.changed.emit()
            self._abort_checks = 0
            self._abort_timer.start()

    def _check_abort(self) -> None:
        """After a stop: once the driver is idle and no result came, the capture is over."""
        if not self._running:
            self._abort_timer.stop()  # its result arrived (a stream)
            return
        self._abort_checks += 1
        if self.driver.is_capturing and self._abort_checks < ABORT_CHECKS:
            return
        if not self.driver.is_capturing and self._abort_checks < 2:
            return  # a result on its way through the event queue gets one more turn
        self._abort_timer.stop()
        self._running = False
        self.received = None
        if self.view is not None:
            self.view.capture_aborted()
        self.changed.emit()
        self.state_changed.emit()

    def arm_and_then(self, action: Callable[[], None], session: Optional[CaptureSession] = None) -> bool:
        """Stimulus and capture: start a capture (``session``, else the settings of the last capture,
        else the quick settings) and run ``action`` once the device is armed."""
        if self.is_capturing:
            return False
        view = self.ensure_view()
        if session is None and view is not None and view.model.session is not None:
            session = view.model.session.clone_settings()
        if session is None and self.last_session is not None:
            session = self.last_session.clone_settings()
        if session is None and self.quick_settings is not None:
            session = self.quick_settings()
        if session is None:
            session = self.stored_settings()
        if session is None or not self.capture(session):
            return False
        # the device is armed once its start command went through (its latency)
        QTimer.singleShot(ARM_DELAY_MS, action)
        return True

    def _progress(self, args) -> None:
        acquisition = self.acquisition
        if acquisition is not None and self.last_session is not None and args.session is self.last_session:
            from ...core import timing_tools

            timing_tools.arrived(acquisition, args)
        if self.view is not None:
            self.view._queue_capture_progress(args)
        total = max(args.session.total_samples, 1) if args.session is not None else 0
        received = max((len(samples) for samples in args.samples.values()), default=0) if args.samples else 0
        fraction = min(received / total, 1.0) if total else None
        if fraction != self.received:
            self.received = fraction
            self.state_changed.emit()

    def state_text(self) -> tuple[str, str]:
        """What the capture does now, for people, and its kind: ``idle``, ``armed``, ``busy``, ``error``."""
        if self._running and self.is_capturing:
            if not self.received:
                return "Armed, waiting for the trigger", "armed"
            return f"Receiving {self.received * 100:.0f} %", "busy"
        if self.error:
            return f"Capture failed: {self.error}", "error"
        if self.last_session is not None:
            return f"Last capture: {self.last_session.total_samples:,} samples", "idle"
        return "Ready", "idle"

    def _completed(self, args) -> None:
        acquisition, self.acquisition = self.acquisition, None
        if acquisition is not None:
            from ...core import timing_tools

            timing_tools.finish(acquisition, self.instrument, args)
        self._running = False  # the driver may still say "capturing" while it hands the result over
        self.received = None
        self.error = "" if getattr(args, "success", False) else (getattr(args, "error", "") or "no samples were received")
        if self.error and connection_error(self.error):
            self.connection_lost.emit(self.error)
        self.state_changed.emit()
        if self.view is None and getattr(args, "success", False):
            self.ensure_view()  # its data view was closed while capturing: the result opens a new one
        if self.view is not None:
            self.view._on_capture_completed(args)
        self.changed.emit()

    def generated_capture(self, parent: Optional[QWidget] = None) -> bool:
        """Test signals generated by the board itself (devices that report ``SIMULATION``)."""
        from ..dialogs.simulation_dialog import SimulationDialog

        if self.is_capturing or not self.can_simulate_on_board():
            return False
        dialog = SimulationDialog(self.driver, on_board=True, parent=parent)
        if not dialog.exec() or dialog.session is None:
            return False
        view = self.ensure_view()
        if view is not None:
            view.expect_simulation(dialog)
        return self.capture(dialog.session)

    # ---------------------------------------------------------- device
    def show_device_info(self, parent: Optional[QWidget] = None, tab: str = "overview") -> None:
        """The device information; the self-test is one of its tabs."""
        from ..dialogs.device_dialogs import DeviceInfoDialog

        if self.is_capturing:
            return
        # The power poll would talk to the device in the middle of a self-test.
        polling = self.power_timer.isActive()
        self.power_timer.stop()
        try:
            if tab == "overview":
                DeviceInfoDialog(self.driver, parent).exec()
            else:
                DeviceInfoDialog(self.driver, parent, initial_tab=tab).exec()
        finally:
            if polling:
                self.power_timer.start()

    def run_board_test(self, parent: Optional[QWidget] = None) -> None:
        self.show_device_info(parent, "self-test")

    def update_network_settings(self, parent: Optional[QWidget] = None) -> bool:
        from ..dialogs.device_dialogs import NetworkSettingsDialog

        dialog = NetworkSettingsDialog(parent)
        if not dialog.exec():
            return False
        if self.driver.send_network_config(dialog.access_point, dialog.password, dialog.address, dialog.port):
            messages.info(parent, "Network settings", "The network settings were saved on the device.",
                          "Restart the device to connect it to the access point.")
            return True
        messages.error(parent, "Network settings", "The network settings could not be saved.",
                       "Restart the device and try again.")
        return False

    def enter_bootloader(self, parent: Optional[QWidget] = None,
                         release: Optional[Callable[[], None]] = None) -> bool:
        if self.is_capturing:
            return False
        if not messages.confirm(parent, "Enter bootloader mode", "Restart the device into bootloader mode?",
                                "Restart into bootloader",
                                "The device is disconnected and appears as a USB drive, ready for a new firmware."):
            return False
        if self.driver.enter_bootloader():
            if release is not None:
                release()
            messages.info(parent, "Enter bootloader mode", "The device is in bootloader mode.",
                          "Use Devices > Update firmware to flash a new firmware.")
            return True
        messages.error(parent, "Enter bootloader mode", "The device did not enter bootloader mode.",
                       "Unplug it and plug it in again while holding the BOOTSEL button.")
        return False

    def start_power_polling(self) -> None:
        if self.driver.is_network:
            self.power_timer.start()
            self.update_power_status()

    def update_power_status(self) -> None:
        """Ask a network device for its power, in a thread of its own: a device that is gone
        answers after seconds, and the window must not wait for that."""
        if not self.driver.is_network or self.is_capturing or self._power_busy:
            return
        self._power_busy = True
        driver = self.driver

        def ask() -> None:
            try:
                status = driver.get_voltage_status()
            except Exception:  # noqa: BLE001 - a device that fails here does not answer
                status = "DISCONNECTED"
            try:
                self._power_answer.emit(status)
            except RuntimeError:  # the controller is gone meanwhile
                pass

        threading.Thread(target=ask, name="openscilab-power", daemon=True).start()

    def _show_power(self, status) -> None:
        self._power_busy = False
        if status == "DISCONNECTED":
            # the regular question is also how a network device that vanished is noticed
            self.connection_lost.emit("the device does not answer")
            return
        if not status or status == "UNSUPPORTED":
            return
        parts = status.split("_")
        if len(parts) == 2:
            source = "external power" if parts[1] == "1" else "battery"
            self.power_text = f"{parts[0]} V ({source})"
            self.changed.emit()

    # ---------------------------------------------------------- profiles
    def fill_profiles_menu(self, menu: QMenu, parent: Optional[QWidget] = None) -> None:
        """Save the current settings; your profiles (load/edit/delete each one); the standard profiles
        (load at a click; those the device has too few channels for are off); the profiles folder,
        import and export."""
        menu.clear()
        self.profiles.reload_folder()  # files put into the profiles folder show up at once
        add_action = menu.addAction(icon("bookmark"), "&Save current settings as profile...")
        add_action.triggered.connect(lambda: self.add_profile(parent))
        menu.addSection("My profiles")
        mine = self.profiles.user_profiles()
        if not mine:
            menu.addAction("No saved profiles").setEnabled(False)
        for profile in mine:
            submenu = menu.addMenu(profile.name.replace("&", "&&"))
            problem = profile_problem(profile, self.driver)
            load_action = submenu.addAction(icon("check"), f"Load ({problem})" if problem else "Load")
            load_action.setEnabled(not self.is_capturing and not problem)
            load_action.triggered.connect(lambda _checked=False, p=profile: self.load_profile(p, parent))
            submenu.addAction(icon("pencil"), "Edit...").triggered.connect(
                lambda _checked=False, p=profile: self.edit_profile(p, parent))
            submenu.addSeparator()
            submenu.addAction(icon("trash"), "Delete...").triggered.connect(
                lambda _checked=False, p=profile: self.delete_profile(p, parent))
        standard = [profile for profile in self.profiles.profiles if profile.standard]
        if standard:
            submenu = menu.addMenu(icon("bus"), "Standard profiles")
            self.standard_menu = submenu
            submenu.setToolTipsVisible(True)
            for profile in sorted(standard, key=lambda candidate: candidate.name.lower()):
                problem = profile_problem(profile, self.driver)
                action = submenu.addAction(profile.name.replace("&", "&&")
                                           + (f"  ({problem})" if problem else ""))
                action.setToolTip(profile.notes)
                action.setEnabled(not self.is_capturing and not problem)
                action.triggered.connect(lambda _checked=False, p=profile: self.load_profile(p, parent))
        menu.addSeparator()
        menu.addAction(icon("folder"), "Open the profiles folder").triggered.connect(self.open_profiles_folder)
        menu.addAction(icon("import"), "&Import profiles...").triggered.connect(lambda: self.import_profiles(parent))
        export_action = menu.addAction(icon("export"), "&Export profiles...")
        export_action.triggered.connect(lambda: self.export_profiles(parent))
        export_action.setEnabled(bool(mine))

    def open_profiles_folder(self) -> None:
        """Show the folder whose profiles are loaded without importing them."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.profiles.folder or profiles_directory()))

    def current_settings(self) -> Optional[CaptureSession]:
        """Settings of the capture the view shows, or the last ones used for the device."""
        view = self.view
        if view is not None and view.model.session is not None and view.model.session.capture_channels:
            return view.model.session.clone_settings()
        return self.stored_settings()

    def _decoders(self) -> list:
        return self.view.provider.to_list() if self.view is not None else []

    def _save_profiles(self, parent) -> None:
        if not self.profiles.save():
            messages.error(parent, "Profiles", "The profiles file could not be written.")

    def add_profile(self, parent: Optional[QWidget] = None) -> bool:
        capture_settings = self.current_settings()
        decoders = self._decoders()
        if capture_settings is None and not decoders:
            messages.info(parent, "Save profile", "There is nothing to store yet.",
                          "Configure a capture or add a protocol decoder first.")
            return False
        name, ok = QInputDialog.getText(parent, "Save profile", "Name of the profile (capture settings and decoders):")
        name = name.strip()
        if not ok or not name:
            return False
        if self.profiles.get(name) is not None and not messages.confirm(
                parent, "Save profile", f'A profile named "{name}" already exists.', "Replace"):
            return False
        self.profiles.add(Profile(name=name, capture_settings=capture_settings, decoder_configuration=decoders))
        self._save_profiles(parent)
        return True

    def load_profile(self, profile: Profile, parent: Optional[QWidget] = None) -> bool:
        from ..dialogs.capture_dialog import capture_settings_file

        if self.is_capturing:
            if not messages.confirm(parent, "Load profile", "A capture is in progress.", "Stop capture and load",
                                    "The running capture is aborted before the profile is loaded."):
                return False
            self.abort()
        problem = profile_problem(profile, self.driver)
        if problem:
            messages.error(parent, "Load profile", f'The profile "{profile.name}" does not fit this device: {problem}.')
            return False
        changes: list[str] = []
        if profile.capture_settings is not None:
            # the next capture of this device starts from the profile (not those of other kinds),
            # within what the device can do
            session, changes = fit_session(profile.capture_settings, self.driver)
            data = capture_io.session_to_dict(session, include_samples=False)
            settings.persist_settings(capture_settings_file(self.driver), data)
            self.profile_loaded.emit()
        decoders = bool(profile.decoder_configuration)
        if decoders and self.view is not None:
            self.view.decoder_manager.load_configuration(profile.decoder_configuration)
        elif decoders:
            self.pending_decoders = list(profile.decoder_configuration)
        text = (f'Profile "{profile.name}" loaded'
                + (", its capture settings apply to the next capture" if profile.capture_settings is not None else "")
                + (f" (for this device: {', '.join(changes)})" if changes else "")
                + (", its decoders to the next data view of the device" if decoders and self.view is None else ""))
        bar = self.view.statusBar() if self.view is not None else _status_bar(parent)
        if bar is not None:
            bar.showMessage(text, 8000)
        self.changed.emit()
        return True

    def delete_profile(self, profile: Profile, parent: Optional[QWidget] = None) -> bool:
        if not messages.confirm(parent, "Delete profile", f'Delete the profile "{profile.name}"?', "Delete",
                                "This cannot be undone.", destructive=True):
            return False
        self.profiles.remove(profile.name)
        self._save_profiles(parent)
        return True

    def edit_profile(self, profile: Profile, parent: Optional[QWidget] = None) -> bool:
        from ..dialogs.profile_dialog import ProfileEditDialog

        others = [candidate.name for candidate in self.profiles.profiles if candidate is not profile]
        dialog = ProfileEditDialog(profile, self._decoders(), others, parent)
        if not dialog.exec() or dialog.profile is None:
            return False
        dialog.profile.source, dialog.profile.standard = profile.source, profile.standard  # stays in its file
        self.profiles.remove(profile.name)
        self.profiles.add(dialog.profile)
        self._save_profiles(parent)
        return True

    def export_profiles(self, parent: Optional[QWidget] = None) -> Optional[str]:
        if not self.profiles.user_profiles():
            return None
        path, _ = QFileDialog.getSaveFileName(parent, "Export profiles", "profiles.json", PROFILE_FILE_FILTER)
        if not path:
            return None
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            write_profiles_file(path, self.profiles.user_profiles())
        except OSError as error:
            messages.error(parent, "Export profiles", "The profiles could not be exported.", str(error))
            return None
        return path

    def import_profiles(self, parent: Optional[QWidget] = None) -> int:
        path, _ = QFileDialog.getOpenFileName(parent, "Import profiles", "", PROFILE_FILE_FILTER)
        if not path:
            return 0
        try:
            imported = read_profiles_file(path)
        except (OSError, ValueError) as error:
            messages.error(parent, "Import profiles", "The profiles could not be imported.", str(error))
            return 0
        conflicts = [profile.name for profile in imported if self.profiles.get(profile.name)]
        replace = True
        if conflicts:
            choice = messages.choose(parent, "Import profiles", f"{len(conflicts)} imported profile(s) already exist.",
                                     ["Replace existing", "Keep existing"], "\n".join(conflicts))
            if choice is None:
                return 0
            replace = choice == 0
        added = 0
        for profile in imported:
            if profile.name in conflicts and not replace:
                continue
            self.profiles.add(profile)
            added += 1
        self._save_profiles(parent)
        return added


def capture_controller(instrument: Instrument) -> Optional[CaptureController]:
    """The capture controller of ``instrument`` (made the first time; ``None`` without a capture facet)."""
    if instrument.capture is None:
        return None
    controller = getattr(instrument, "capture_controller", None)
    if controller is None:
        controller = CaptureController(instrument)
        instrument.capture_controller = controller
    return controller


def _status_bar(parent: Optional[QWidget]):
    """The status bar of the window of ``parent`` (``None`` when it has none)."""
    window = parent.window() if parent is not None else None
    status_bar = getattr(window, "statusBar", None)
    return status_bar() if callable(status_bar) else None
