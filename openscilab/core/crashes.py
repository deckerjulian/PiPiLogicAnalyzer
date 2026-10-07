# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""What is left of an error nobody caught: ``crash.log`` and ``faults.log`` in the settings
directory.

:func:`install` hooks the exceptions that reach the top of a thread (``sys.excepthook``,
``threading.excepthook``) - in the application mostly a slot of Qt that failed, which Qt would
otherwise print to a console nobody sees - and writes them with their traceback to ``crash.log``;
a ``notify`` callback tells the user. The same error raised again and again (a timer) is counted,
not written again. ``faulthandler`` writes the stacks of a crash of the interpreter itself (a
segmentation fault, a deadlock the OS kills) to ``faults.log``; the device processes write there
too. Qt free.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from typing import Callable, Optional

from . import settings

log = logging.getLogger(__name__)

CRASH_LOG = "crash.log"
FAULTS_LOG = "faults.log"
#: the log is cut to its newest part when it grows beyond this (bytes)
MAX_SIZE = 512 * 1024
#: the same traceback within this time is counted, not written (seconds)
REPEAT_WINDOW = 5.0

_installed: dict = {}
_faults_file = None


def crash_log_path() -> str:
    return settings.settings_path(CRASH_LOG)


def faults_log_path() -> str:
    return settings.settings_path(FAULTS_LOG)


def enable_faulthandler(path: Optional[str] = None) -> Optional[str]:
    """Write the stacks of a crash of the interpreter to ``faults.log`` (every process: the device
    processes call it too). Returns the path, ``None`` when the file cannot be opened."""
    global _faults_file
    path = path or faults_log_path()
    try:
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - held open for the life of the process
    except OSError as error:
        log.debug("faults.log cannot be opened: %s", error)
        return None
    _faults_file = handle  # (faulthandler keeps the descriptor, the object keeps it open)
    faulthandler.enable(handle, all_threads=True)
    return path


def install(path: Optional[str] = None, notify: Optional[Callable[[str], None]] = None,
            faults: bool = True) -> str:
    """Hook the uncaught exceptions of every thread; returns the path of ``crash.log``.

    ``notify(text)`` is called with one line about each new error, from the thread that failed
    (make it thread safe: emit a signal). The default hooks still run afterwards (the traceback
    on the console)."""
    path = path or crash_log_path()
    uninstall()
    state = {"path": path, "notify": notify, "last": ("", 0.0), "repeats": 0,
             "sys": sys.excepthook, "threading": threading.excepthook}

    def hook(kind, value, trace, thread_name: str = "") -> None:
        _record(state, kind, value, trace, thread_name)

    def sys_hook(kind, value, trace) -> None:
        hook(kind, value, trace)
        state["sys"](kind, value, trace)

    def threading_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        hook(args.exc_type, args.exc_value, args.exc_traceback, getattr(args.thread, "name", "") or "thread")
        state["threading"](args)

    sys.excepthook = sys_hook
    threading.excepthook = threading_hook
    state["hooks"] = (sys_hook, threading_hook)
    _installed.update(state)
    if faults:
        enable_faulthandler()
    return path


def uninstall() -> None:
    if _installed:
        sys.excepthook = _installed["sys"]
        threading.excepthook = _installed["threading"]
        _installed.clear()


def installed() -> bool:
    return bool(_installed)


def _record(state: dict, kind, value, trace, thread_name: str) -> None:
    text = "".join(traceback.format_exception(kind, value, trace))
    line = f"{kind.__name__}: {value}".splitlines()[0] if value is not None else kind.__name__
    now = time.monotonic()
    last_text, last_time = state["last"]
    if text == last_text and now - last_time < REPEAT_WINDOW:
        state["repeats"] += 1  # (a timer that fails every tick: counted, not written each time)
        state["last"] = (text, now)
        return
    repeats, state["repeats"] = state["repeats"], 0
    state["last"] = (text, now)
    where = f" in thread {thread_name}" if thread_name else ""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{'=' * 78}\n{stamp}{where}" + (f" (the previous error repeated {repeats} times)" if repeats else "") \
        + f"\n{text}"
    _append(state["path"], entry)
    log.error("Uncaught error%s: %s (see %s)", where, line, state["path"])
    notify = state["notify"]
    if notify is not None:
        try:
            notify(f"{line}{where}")
        except Exception:  # noqa: BLE001 - the hook must never raise
            log.debug("The crash notice could not be shown", exc_info=True)


def _append(path: str, entry: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_SIZE:
            with open(path, encoding="utf-8", errors="replace") as handle:
                handle.seek(os.path.getsize(path) - MAX_SIZE // 2)
                kept = handle.read()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("(older entries cut)\n" + kept)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(entry)
            if not entry.endswith("\n"):
                handle.write("\n")
    except OSError as error:
        log.debug("crash.log cannot be written: %s", error)


def read(path: Optional[str] = None, limit: int = 20_000) -> str:
    """The newest part of ``crash.log`` (empty when there is none)."""
    path = path or crash_log_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    return text[-limit:]
