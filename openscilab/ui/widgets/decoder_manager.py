# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of openSciLab, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Panel that manages the protocol decoders applied to the capture.

Port of ``Controls/SigrokDecoderManager.axaml.cs``.  Decoding runs in the decoder
process (:mod:`openscilab.sigrok.worker`), waited for by a worker thread: a slow decoder
can neither freeze the UI nor stall the threads that record and synchronise (the original
decoded on the UI thread through Python.NET while holding the GIL).
"""

from __future__ import annotations

import copy
import threading
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import colors
from ...driver.models import CaptureSession
from ...sigrok.engine import DecoderRegistry
from ...sigrok.provider import DecoderInstance, SigrokProvider
from .. import messages
from ..dialogs.common import hint
from ..dialogs.decoder_browser import DecoderBrowserDialog
from ..dialogs.decoder_options import DecoderOptionsDialog
from ..icons import set_icon
from ..theme import set_role
from ..view_model import CaptureViewModel


#: milliseconds a closing view waits for its decoders to end
SHUTDOWN_WAIT_MS = 1000

#: Workers that still run. They have no parent: a data view that closes while it decodes must
#: not destroy a running thread (Qt aborts the application then); a worker deletes itself
#: when it is done.
_running_workers: set = set()


class DecodeWorker(QThread):
    """Runs every configured decoder over a capture."""

    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, provider: SigrokProvider, session: CaptureSession) -> None:
        super().__init__()
        # the settings as they are now: the options dialog changes the decoders meanwhile
        self._provider = provider.frozen()
        self._session = session
        self._cancel = threading.Event()
        _running_workers.add(self)
        self.finished.connect(self._ended)

    def cancel(self) -> None:
        """The result is no longer wanted: the decoders end at their next step."""
        self._cancel.set()

    def _ended(self) -> None:
        _running_workers.discard(self)
        self.deleteLater()

    def run(self) -> None:  # noqa: D401 - QThread entry point
        from ...sigrok import worker

        try:
            groups = worker.run(self._provider, self._session, cancelled=self._cancel.is_set)
        except Exception as error:  # noqa: BLE001 - reported to the UI
            if not self._cancel.is_set():
                self.failed.emit(str(error))
            return
        if not self._cancel.is_set():
            self.completed.emit(groups)


class DecoderManager(QWidget):
    """List of decoder instances plus the controls to run them."""

    def __init__(
        self,
        model: CaptureViewModel,
        provider: SigrokProvider,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.model = model
        self.provider = provider
        self._worker: Optional[DecodeWorker] = None
        # Set when a decode is requested while one is running; it runs again once that finishes.
        self._decode_pending = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Protocol decoders", self)
        set_role(title, "heading")
        header.addWidget(title, 1)
        self.status_label = QLabel("", self)
        set_role(self.status_label, "hint")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setToolTip("Double-click a decoder to change its channels and options")
        self.list.itemDoubleClicked.connect(lambda _item: self.edit_selected())
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.currentItemChanged.connect(lambda *_: self._refresh_buttons())
        layout.addWidget(self.list, 1)

        self.empty_label = hint("", self)
        self.empty_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.add_button = QPushButton("Add decoder...", self)
        self.add_button.setToolTip("Add a protocol decoder for the capture channels")
        self.add_button.clicked.connect(self.add_decoder)
        set_icon(self.add_button, "plus")
        buttons.addWidget(self.add_button, 1)

        # The actions on the selected decoder are compact icon buttons.
        self.stack_button = QPushButton(self)
        self.stack_button.setToolTip("Stack: add a decoder that decodes the output of the selected one")
        self.stack_button.clicked.connect(self.stack_decoder)
        self.edit_button = QPushButton(self)
        self.edit_button.setToolTip("Settings: channels and options of the selected decoder")
        self.edit_button.clicked.connect(self.edit_selected)
        self.remove_button = QPushButton(self)
        self.remove_button.setToolTip("Remove the selected decoder")
        self.remove_button.clicked.connect(self.remove_selected)
        for button, name, label in (
            (self.stack_button, "layers", "Stack decoder"),
            (self.edit_button, "sliders", "Decoder settings"),
            (self.remove_button, "trash", "Remove decoder"),
        ):
            button.setAccessibleName(label)
            button.setFixedWidth(34)
            set_icon(button, name)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        run_row = QHBoxLayout()
        self.auto_decode = QCheckBox("Decode automatically", self)
        self.auto_decode.setToolTip("Decode again whenever the capture or a decoder changes")
        self.auto_decode.setChecked(True)
        run_row.addWidget(self.auto_decode, 1)
        self.decode_button = QPushButton("Decode now", self)
        set_icon(self.decode_button, "play")
        self.decode_button.clicked.connect(self.decode)
        run_row.addWidget(self.decode_button)
        layout.addLayout(run_row)

        model.capture_changed.connect(self._on_capture_changed)
        #: decode once a progressive transfer is complete
        self._waiting_for_transfer = False
        self._refresh_buttons()

    # ------------------------------------------------------------- decoders
    @property
    def registry(self) -> DecoderRegistry:
        return self.provider.registry

    def add_decoder(self) -> None:
        dialog = DecoderBrowserDialog(self.registry, self)
        if not dialog.exec() or dialog.selected is None:
            return

        info = dialog.selected
        if info.is_base_decoder:
            self._configure_new(info, parent=None)
            return

        parent = self._parent_for(info)
        if parent is not None:
            self._configure_new(info, parent=parent)

    def _parent_for(self, info) -> Optional[DecoderInstance]:
        """Decoder whose output feeds the stacked decoder ``info``.

        An existing decoder is used when there is one (the user chooses between
        several); otherwise the chain of decoders it needs is added first.
        """
        wanted = set(info.inputs)
        candidates = []
        for instance in self.provider.instances:
            candidate_info = self.provider.info_for(instance)
            if candidate_info is not None and wanted & set(candidate_info.outputs):
                candidates.append(instance)

        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            labels = [
                f"{number + 1}: {candidate.label or candidate.decoder_id}"
                for number, candidate in enumerate(candidates)
            ]
            label, ok = QInputDialog.getItem(
                self, "Add decoder", f"{info.name} decodes the output of:", labels, 0, False
            )
            return candidates[labels.index(label)] if ok else None

        chain = self.registry.provider_chain(info)
        if not chain:
            messages.warning(
                self,
                "Add decoder",
                f"{info.name} cannot be added.",
                f"It needs the output of a '{', '.join(info.inputs)}' decoder, but none is installed.",
            )
            return None

        steps = " → ".join(step.name for step in chain)
        if not messages.confirm(
            self,
            "Add decoder",
            f"Add {steps} first and stack {info.name} on top?",
            f"Add {steps} and {info.name}",
            f"{info.name} decodes the output of another decoder.",
        ):
            return None

        parent: Optional[DecoderInstance] = None
        for step in chain:
            parent = self._configure_new(step, parent=parent)
            if parent is None:
                return None
        return parent

    def stack_decoder(self) -> None:
        instance = self.selected_instance()
        if instance is None:
            return
        info = self.provider.info_for(instance)
        if info is None or not info.outputs:
            messages.info(
                self,
                "Stack decoder",
                f"{instance.label or instance.decoder_id} has no output other decoders can use.",
            )
            return

        dialog = DecoderBrowserDialog(self.registry, self, only_inputs=set(info.outputs))
        if not dialog.exec() or dialog.selected is None:
            return
        self._configure_new(dialog.selected, parent=instance)

    def _configure_new(
        self, info, parent: Optional[DecoderInstance]
    ) -> Optional[DecoderInstance]:
        instance = DecoderInstance(
            decoder_id=info.id,
            label=info.name,
            options=info.default_options(),
            color_index=len(self.provider.instances),
            parent=parent,
        )
        self._auto_assign_channels(info, instance)

        dialog = DecoderOptionsDialog(info, instance, self.model.channels, self)
        if not dialog.exec():
            return None

        self.provider.add_instance(instance)
        self.refresh()
        self.decode_if_automatic()
        return instance

    def _auto_assign_channels(self, info, instance: DecoderInstance) -> None:
        """Pre-assign capture channels whose name matches the decoder channel."""
        channels = self.model.channels
        by_name = {
            (channel.channel_name or "").strip().lower(): channel.channel_number
            for channel in channels
            if channel.channel_name
        }
        used: set[int] = set()
        for decoder_channel in info.channels:
            number = by_name.get(decoder_channel.id.lower())
            if number is None:  # not "or": capture channel 0 is a valid match
                number = by_name.get(decoder_channel.name.lower())
            if number is not None and number not in used:
                instance.channel_map[decoder_channel.index] = number
                used.add(number)

        # Fall back to the first free channels for the required inputs.
        for decoder_channel in info.required_channels:
            if decoder_channel.index in instance.channel_map:
                continue
            for channel in channels:
                if channel.channel_number not in used:
                    instance.channel_map[decoder_channel.index] = channel.channel_number
                    used.add(channel.channel_number)
                    break

    def selected_instance(self) -> Optional[DecoderInstance]:
        item = self.list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def edit_selected(self) -> None:
        instance = self.selected_instance()
        if instance is None:
            return
        info = self.provider.info_for(instance)
        if info is None:
            return
        dialog = DecoderOptionsDialog(info, instance, self.model.channels, self)
        if dialog.exec():
            self.refresh()
            self.decode_if_automatic()

    def remove_selected(self) -> None:
        instance = self.selected_instance()
        if instance is None:
            return
        self.provider.remove_instance(instance)
        self.refresh()
        self.decode_if_automatic()

    def load_configuration(self, configuration) -> None:
        """Replace every decoder with a stored configuration (profiles)."""
        self.provider.load_configuration(configuration)
        self.refresh()
        self.decode_if_automatic()

    # ----------------------------------------------------------------- state
    def refresh(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for instance in self.provider.instances:
            info = self.provider.info_for(instance)
            name = instance.label or instance.decoder_id
            if instance.parent is not None:
                name = f"↳ {name}"
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, instance)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if instance.enabled else Qt.Unchecked)
            item.setForeground(colors.get_color(instance.color_index))
            if info is None:
                item.setText(f"{name}  (not installed)")
                item.setToolTip(f"Decoder '{instance.decoder_id}' is not installed")
            else:
                missing = self.provider.missing_channels(instance)
                # assigned to channels this capture does not have
                missing += self.provider.unavailable_channels(instance, self.model.channels)
                if missing:
                    item.setText(f"{name}  (channels missing)")
                item.setToolTip(
                    info.longname
                    + ("\nMissing channels: " + ", ".join(missing) if missing else "")
                )
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.list.count() and self.list.currentItem() is None:
            self.list.setCurrentRow(self.list.count() - 1)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        has_capture = self.model.sample_count > 0
        has_decoders = bool(self.provider.instances)
        selected = self.list.currentItem() is not None
        self.add_button.setEnabled(has_capture)
        self.stack_button.setEnabled(has_capture and selected)
        self.edit_button.setEnabled(has_decoders and selected)
        self.remove_button.setEnabled(has_decoders and selected)
        self.decode_button.setEnabled(has_capture and has_decoders)

        if has_decoders:
            self.empty_label.setVisible(False)
        else:
            self.empty_label.setText(
                "Add a decoder for UART, SPI, I²C and more."
                if has_capture
                else "Load or capture signals to decode protocols."
            )
            self.empty_label.setVisible(True)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        instance = item.data(Qt.UserRole)
        if instance is None:
            return
        instance.enabled = item.checkState() == Qt.Checked
        self.decode_if_automatic()

    def _on_capture_changed(self) -> None:
        self._refresh_buttons()
        if self._waiting_for_transfer and self.model.progressive is None:
            self._waiting_for_transfer = False
            self.decode()
            return
        self.decode_if_automatic()

    # -------------------------------------------------------------- decoding
    def decode_if_automatic(self) -> None:
        # A live capture is decoded once it is complete, one recorded to disk only on request
        # (the decoders would read all of it).
        if self.auto_decode.isChecked() and not self.model.is_live and not self.model.on_disk:
            self.decode()

    def decode(self) -> None:
        session = self.model.session
        if session is None or not self.provider.instances:
            self.model.set_annotation_groups([])
            self.status_label.setText("")
            return

        if self._worker is not None:
            # what the running decoders produce is outdated: end them and decode again
            self._worker.cancel()
            self._decode_pending = True
            return
        self._decode_pending = False
        if self.model.progressive is not None:
            # A capture still arriving: decode it once all of it is there.
            self._waiting_for_transfer = True
            self.status_label.setText("Decoding when the transfer is complete...")
            set_role(self.status_label, "hint")
            return

        self.status_label.setText("Decoding...")
        set_role(self.status_label, "hint")
        self.decode_button.setEnabled(False)

        # The sample editor replaces the channel arrays while the worker reads them: decode a
        # snapshot of the channel list (the arrays themselves are never changed in place).
        snapshot = copy.copy(session)
        snapshot.capture_channels = [copy.copy(channel) for channel in session.capture_channels]

        self._worker = DecodeWorker(self.provider, snapshot)
        self._worker.completed.connect(self._on_decoding_completed)
        self._worker.failed.connect(self._on_decoding_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        self._worker = None  # it deletes itself
        self._refresh_buttons()
        if self._decode_pending:
            self.decode()

    @property
    def decoding(self) -> bool:
        return self._worker is not None

    def shutdown(self) -> None:
        """The view closes: a running decode is ended and no longer reports here."""
        worker, self._worker = self._worker, None
        self._decode_pending = False
        if worker is None:
            return
        for signal, slot in ((worker.completed, self._on_decoding_completed),
                             (worker.failed, self._on_decoding_failed),
                             (worker.finished, self._on_worker_finished)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        worker.cancel()
        worker.wait(SHUTDOWN_WAIT_MS)  # ends at the next step of the decoder

    def _on_decoding_completed(self, groups: list) -> None:
        if self._decode_pending:
            return  # outdated, the pending decode replaces it
        errors = [group for group in groups if group.error]
        rows = sum(group.row_count for group in groups)
        if errors:
            self.status_label.setText(f"{len(errors)} decoder(s) failed")
            set_role(self.status_label, "error")
            self.status_label.setToolTip(
                "\n".join(f"{group.decoder_name}: {group.error}" for group in errors)
            )
        else:
            self.status_label.setText(f"{rows} annotation row(s)")
            set_role(self.status_label, "hint")
            self.status_label.setToolTip("")

        self.model.set_annotation_groups(groups)

    def _on_decoding_failed(self, message: str) -> None:
        self.status_label.setText("Decoding failed")
        set_role(self.status_label, "error")
        self.status_label.setToolTip(message)
