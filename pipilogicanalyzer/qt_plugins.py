# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Work around Qt plugins that macOS marks as hidden files.

Qt skips hidden files when it scans its plugin directories and then aborts with
"Could not find the Qt platform plugin". iCloud Drive ("Desktop & Documents")
sets the ``hidden`` flag on the files of a virtual environment such as ``.venv``
and restores it after ``chflags nohidden``. When that is the case the plugins
are copied, without the flag, to a cache directory and Qt is pointed there.

Call :func:`ensure_loadable_plugins` before the ``QApplication`` is created.
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import MutableMapping, Optional

#: Plugin groups the application uses (the platform plugin is mandatory).
PLUGIN_GROUPS = ("platforms", "styles", "imageformats", "iconengines")

DEFAULT_CACHE = os.path.expanduser("~/Library/Caches/PiPiLogicAnalyzer/qt-plugins")


def plugin_directory() -> str:
    from PySide6.QtCore import QLibraryInfo

    return QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)


def hidden_files(directory: str) -> list[str]:
    """Files in ``directory`` carrying the ``hidden`` flag."""
    if not hasattr(stat, "UF_HIDDEN"):
        return []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    hidden = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            if getattr(os.lstat(path), "st_flags", 0) & stat.UF_HIDDEN:
                hidden.append(path)
        except OSError:
            continue
    return hidden


def _clear_hidden_flag(path: str) -> None:
    flags = getattr(os.lstat(path), "st_flags", 0)
    if flags & stat.UF_HIDDEN:
        os.chflags(path, flags & ~stat.UF_HIDDEN)


def ensure_loadable_plugins(
    source: Optional[str] = None,
    cache: Optional[str] = None,
    environ: MutableMapping[str, str] = os.environ,
) -> Optional[str]:
    """Make the Qt plugins loadable; return the cache directory if one is used.

    Raises ``OSError`` when the plugins are hidden and cannot be copied.
    """
    if not hasattr(stat, "UF_HIDDEN") or not hasattr(os, "chflags"):
        return None

    source = source or plugin_directory()
    if not hidden_files(os.path.join(source, "platforms")):
        return None

    target = cache or os.path.join(DEFAULT_CACHE, _qt_version_tag())
    for group in PLUGIN_GROUPS:
        source_group = os.path.join(source, group)
        if not os.path.isdir(source_group):
            continue
        target_group = os.path.join(target, group)
        os.makedirs(target_group, exist_ok=True)
        _clear_hidden_flag(target_group)

        for name in os.listdir(source_group):
            source_file = os.path.join(source_group, name)
            if not os.path.isfile(source_file):
                continue
            target_file = os.path.join(target_group, name)
            if (
                not os.path.exists(target_file)
                or os.path.getsize(target_file) != os.path.getsize(source_file)
            ):
                # copyfile + copymode: copystat would copy the hidden flag as well
                shutil.copyfile(source_file, target_file)
                shutil.copymode(source_file, target_file)
            _clear_hidden_flag(target_file)

    environ["QT_PLUGIN_PATH"] = target
    environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(target, "platforms")
    return target


def _qt_version_tag() -> str:
    try:
        import PySide6

        return PySide6.__version__
    except (ImportError, AttributeError):
        return "unknown"
