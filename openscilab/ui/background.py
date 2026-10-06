# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Slow work with devices off the user interface thread.

Opening a device can take long: a serial port that does not answer, a network address that is
not there, a firmware or bitstream upload. :func:`run` does such a call in a worker thread and
returns its result as if it had been called directly; meanwhile the window keeps drawing and a
small dialog says what is being waited for. *Cancel* stops the waiting – the call itself ends in
the background and what it returns is released.

Calls that end at once (simulators, drivers in tests) never show the dialog.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, TypeVar

from PySide6.QtCore import QEventLoop, Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

log = logging.getLogger(__name__)

T = TypeVar("T")

#: seconds a call may take before the window starts waiting for it with an event loop
FAST_S = 0.05
#: milliseconds before the dialog appears
DIALOG_AFTER_MS = 400
POLL_MS = 25


class Cancelled(Exception):
    """The user stopped waiting for a call of :func:`run`."""


class _Waiting(QProgressDialog):
    """The dialog of :func:`run`. A wait that cannot be cancelled (a file being written is
    replaced later all the same) has no *Cancel* – and closing the window by its title bar or
    with Escape must not end it either."""

    cancellable = True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.cancellable:
            super().closeEvent(event)
        else:
            event.ignore()

    def reject(self) -> None:
        if self.cancellable:
            super().reject()


def _release(value: Any) -> None:
    """Close what an abandoned call returned (a driver, an instrument)."""
    for name in ("close", "dispose"):
        method = getattr(value, name, None)
        if callable(method):
            try:
                method()
            except Exception:  # noqa: BLE001 - nobody waits for it any more
                log.debug("Releasing the result of an abandoned call failed", exc_info=True)
            return


def run(parent: Optional[QWidget], text: str, call: Callable[[], T], cancellable: bool = True) -> T:
    """``call()`` in a worker thread; returns its result or raises what it raised.

    Raises :class:`Cancelled` when the user cancels the waiting. Outside the user interface
    thread (or without an application) the call is simply made.
    """
    application = QApplication.instance()
    if application is None or QThread.currentThread() is not application.thread():
        return call()

    outcome: dict[str, Any] = {}
    done = threading.Event()
    abandoned = threading.Event()

    def work() -> None:
        try:
            outcome["value"] = call()
        except BaseException as error:  # noqa: BLE001 - raised again in the caller
            outcome["error"] = error
        finally:
            done.set()
            if abandoned.is_set() and "value" in outcome:
                _release(outcome["value"])

    thread = threading.Thread(target=work, name="openscilab-device-call", daemon=True)
    thread.start()
    if not done.wait(FAST_S):
        _wait(parent, text, done, cancellable)
        if not done.is_set():
            abandoned.set()
            if done.is_set() and "value" in outcome:  # it ended just now
                _release(outcome["value"])
            raise Cancelled(text)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _wait(parent: Optional[QWidget], text: str, done: threading.Event, cancellable: bool) -> None:
    """Keep the window alive until ``done`` is set (or the user cancels)."""
    loop = QEventLoop()
    dialog = _Waiting(text, "Cancel" if cancellable else "", 0, 0, parent)
    dialog.cancellable = cancellable
    dialog.setWindowTitle("openSciLab")
    dialog.setWindowModality(Qt.WindowModal if parent is not None else Qt.ApplicationModal)
    dialog.setMinimumDuration(DIALOG_AFTER_MS)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    if not cancellable:
        dialog.setCancelButton(None)
    else:
        dialog.canceled.connect(loop.quit)
    timer = QTimer()
    timer.setInterval(POLL_MS)
    timer.timeout.connect(lambda: done.is_set() and loop.quit())
    timer.start()
    QApplication.setOverrideCursor(Qt.BusyCursor)
    try:
        dialog.setValue(0)  # starts the minimum duration
        loop.exec()
    finally:
        QApplication.restoreOverrideCursor()
        timer.stop()
        if cancellable:
            dialog.canceled.disconnect(loop.quit)
        dialog.cancellable = True  # (it may close now)
        dialog.close()
        dialog.deleteLater()
