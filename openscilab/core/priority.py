# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A higher priority for the processes that read devices (preference ``devices.priority``).

The device processes (``driver/process``) start from the application and take over its priority.
Windows lets a process raise itself above normal without rights (the device processes do). macOS
and Linux let a process lower its priority, not raise it: that takes the rights of an
administrator, which :func:`raise_with_rights` asks for - with the password dialog of macOS
(``osascript ... with administrator privileges``) or of polkit on Linux (``pkexec``) - and gives the
application and its running device processes the *nice* value :data:`NICE`; device processes
started later take it over. On Linux :func:`allow_permanently` instead lets the user raise it
without asking (a line in ``/etc/security/limits.d``, from the next login on). The decoder process
lowers itself again (:func:`lower_own`): decoding must not compete with reading.

Qt free.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from typing import Optional

#: the nice value of the processes that read devices (-20 highest, 0 normal, 19 lowest)
NICE = -10
#: the file of :func:`allow_permanently` (Linux)
LIMITS_FILE = "/etc/security/limits.d/90-openscilab.conf"


def supported() -> bool:
    return sys.platform == "win32" or hasattr(os, "setpriority")


def current(pid: int = 0) -> Optional[int]:
    """The nice value of ``pid`` (0: this process); ``None`` where there is none (Windows)."""
    if not hasattr(os, "getpriority"):
        return None
    try:
        return os.getpriority(os.PRIO_PROCESS, pid)
    except OSError:
        return None


def is_raised(pid: int = 0) -> bool:
    value = current(pid)
    return value is not None and value <= NICE


def raise_own(nice: int = NICE) -> bool:
    """Raise this process without asking: Windows (above normal), or where the rights allow it
    (root, ``CAP_SYS_NICE``, a limit of :func:`allow_permanently`). ``False`` when it may not."""
    if sys.platform == "win32":
        try:
            import ctypes

            above_normal = 0x8000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), above_normal))
        except Exception:  # noqa: BLE001 - normal priority then
            return False
    if not hasattr(os, "setpriority"):
        return False
    try:
        os.setpriority(os.PRIO_PROCESS, 0, nice)
    except OSError:
        return False
    return is_raised()


def lower_own(nice: int = 0) -> None:
    """Back to ``nice`` (a process that took over the raised priority but has no need of it); always
    allowed."""
    value = current()
    if value is not None and value < nice:
        try:
            os.setpriority(os.PRIO_PROCESS, 0, nice)
        except OSError:
            pass


def device_pids() -> list[int]:
    """The running device processes of this application."""
    import multiprocessing

    return [child.pid for child in multiprocessing.active_children()
            if child.pid and child.name.startswith("openscilab-device")]


def rights_command(pids: list[int], nice: int = NICE) -> Optional[list[str]]:
    """The command that raises ``pids`` with the rights of an administrator, asking for them; ``None``
    where there is no way to ask (Linux without ``pkexec``, Windows: not needed)."""
    numbers = " ".join(str(int(pid)) for pid in pids)
    if sys.platform == "darwin":
        script = (f'do shell script "/usr/bin/renice -n {int(nice)} -p {numbers}" with administrator privileges '
                  'with prompt "openSciLab reads devices with a higher priority, so that a busy computer does not '
                  'make a stream overflow."')
        return ["/usr/bin/osascript", "-e", script]
    if sys.platform.startswith("linux"):
        pkexec, renice = shutil.which("pkexec"), shutil.which("renice")
        if pkexec is None or renice is None:
            return None
        return [pkexec, renice, "-n", str(int(nice)), "-p", *[str(int(pid)) for pid in pids]]
    return None


class PriorityError(RuntimeError):
    """The priority could not be raised (the message says why, for people)."""


def raise_with_rights(pids: Optional[list[int]] = None, nice: int = NICE, timeout: float = 120.0) -> list[int]:
    """Raise this application and its device processes (or ``pids``), asking for the administrator's
    password; returns the processes that run raised now. Raises :class:`PriorityError` when the user
    cancelled or the system has no way to ask."""
    pids = list(pids) if pids is not None else [os.getpid(), *device_pids()]
    command = rights_command(pids, nice)
    if command is None:
        raise PriorityError("this system has no way to ask for the rights (on Linux: install polkit, pkexec)")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PriorityError(str(error)) from None
    raised = [pid for pid in pids if is_raised(pid)]
    if not raised:
        reason = (result.stderr or result.stdout or "").strip().splitlines()
        cancelled = result.returncode in (1, 126, 127) or any("cancel" in line.lower() for line in reason)
        raise PriorityError("not allowed (cancelled)" if cancelled else (reason[-1] if reason else "not allowed"))
    return raised


def allow_permanently(nice: int = NICE, user: Optional[str] = None) -> None:
    """Linux: let ``user`` raise openSciLab without asking from the next login on (a ``nice`` limit in
    :data:`LIMITS_FILE`, written with the rights of an administrator)."""
    if not sys.platform.startswith("linux"):
        raise PriorityError("only on Linux")
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        raise PriorityError("pkexec (polkit) is needed")
    user = user or getpass.getuser()
    line = f"{user} - nice {int(nice)}"
    script = f"printf '%s\\n' '# openSciLab: device processes with a higher priority' '{line}' > {LIMITS_FILE}"
    command = [pkexec, "/bin/sh", "-c", script]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PriorityError(str(error)) from None
    if result.returncode != 0:
        raise PriorityError((result.stderr or "not allowed").strip())


def describe(pid: int = 0) -> str:
    """The priority of ``pid`` for people (*Details* of a device)."""
    value = current(pid)
    if value is None:
        return "above normal" if sys.platform == "win32" else "normal"
    return f"{'raised' if value < 0 else 'normal' if value == 0 else 'lowered'} (nice {value})"
