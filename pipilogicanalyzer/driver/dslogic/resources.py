# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The FPGA bitstreams (and FX2 firmware) of the DSLogic boards.

They are not part of PiPiLogicAnalyzer: the design sources are not published and whether the
binaries may be redistributed is unclear. They are taken from an installed DSView, from a folder
the user chose, or downloaded once from the DSView repository into the settings directory.
"""

from __future__ import annotations

import glob
import hashlib
import os
import sys
import urllib.request
from typing import Callable, Iterable, Optional

from ...core import settings

SETTINGS_FILE = "dslogic.json"
CACHE_DIRECTORY = "dslogic"

#: DSView commit the downloaded files come from, and their checksums
DSVIEW_COMMIT = "2e9e2c8e726df4ef5687d39b83d4f797cc44b574"
DOWNLOAD_URL = "https://raw.githubusercontent.com/DreamSourceLab/DSView/{commit}/DSView/res/{name}"
KNOWN_FILES: dict[str, tuple[int, str]] = {
    "DSLogicPlus.bin": (341160, "6a6f8dc0ed27dbfbd41dd98b030da52d8e2c5c7f840c23ed903c7d5fe38e11dd"),
    "DSLogicPlus-pgl12.bin": (530620, "340413e053a765c38ee87e89f45a55ad2530a98ad132ccb7e8a9a0b0c99d0ee2"),
    "DSLogicPlus-pgl12-2.bin": (530620, "b4ee3d3042d8c7cf3536403407490ffabc7ff0699332da331a04c1e6f0bad067"),
    "DSLogicU2Pro16.bin": (465304, "c0be3b52222a9efea8fc5cc59f579c6880e73e9f103982d2aeb4c443ca64d8c9"),
    "DSLogicU3Pro16.bin": (465304, "0ae5a6e52eb9d6db27c3591d3d0df5c34c48c8509d25c65c6ea1f008f622a56e"),
    "DSLogicU3Pro32.bin": (465304, "0dc3251be0476cf964fa521ce2a34651ae82724e96fd4272cadc0cc27c86a17d"),
}


class ResourceError(OSError):
    """A bitstream could not be found or downloaded."""


def chosen_directory() -> Optional[str]:
    stored = settings.get_settings(SETTINGS_FILE)
    if isinstance(stored, dict) and isinstance(stored.get("resource_directory"), str):
        return stored["resource_directory"]
    return None


def set_chosen_directory(path: Optional[str]) -> None:
    stored = settings.get_settings(SETTINGS_FILE)
    stored = stored if isinstance(stored, dict) else {}
    stored["resource_directory"] = path
    settings.persist_settings(SETTINGS_FILE, stored)


def cache_directory() -> str:
    return os.path.join(settings.settings_directory(), CACHE_DIRECTORY)


def dsview_directories() -> list[str]:
    """``res`` folders of DSView installations in their usual places."""
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.append(os.path.join(base, "DSView", "res"))
                candidates.extend(glob.glob(os.path.join(base, "DreamSourceLab", "DSView*", "res")))
                candidates.extend(glob.glob(os.path.join(base, "Programs", "DSView", "res")))
    elif sys.platform == "darwin":
        for base in ("/Applications", os.path.expanduser("~/Applications")):
            candidates.append(os.path.join(base, "DSView.app", "Contents", "MacOS", "res"))
            candidates.append(os.path.join(base, "DSView.app", "Contents", "Resources", "res"))
    else:
        candidates += [
            "/usr/share/DSView/res",
            "/usr/local/share/DSView/res",
            "/opt/DSView/share/DSView/res",
            os.path.expanduser("~/.local/share/DSView/res"),
        ]
    return candidates


def search_directories() -> list[str]:
    directories: list[str] = []
    chosen = chosen_directory()
    if chosen:
        directories.append(chosen)
    directories.append(cache_directory())
    directories.extend(dsview_directories())
    return directories


def _valid(path: str) -> bool:
    # Other DSView versions ship other builds of the same files: no checksum here.
    return os.path.isfile(path) and os.path.getsize(path) > 0


def find(name: str, directories: Optional[Iterable[str]] = None) -> Optional[str]:
    """Path of resource ``name`` in the first directory that has it."""
    for directory in directories if directories is not None else search_directories():
        path = os.path.join(directory, name)
        if _valid(path):
            return path
    return None


def load(name: str, directories: Optional[Iterable[str]] = None) -> bytes:
    path = find(name, directories)
    if path is None:
        raise ResourceError(f"{name} was not found")
    with open(path, "rb") as handle:
        return handle.read()


def downloadable(name: str) -> bool:
    return name in KNOWN_FILES


def download(
    name: str,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 30.0,
) -> str:
    """Downloads ``name`` from the DSView repository into the cache and checks its checksum."""
    if name not in KNOWN_FILES:
        raise ResourceError(f"{name} cannot be downloaded")
    size, sha256 = KNOWN_FILES[name]
    url = DOWNLOAD_URL.format(commit=DSVIEW_COMMIT, name=name)
    try:
        with opener(url, timeout=timeout) as response:  # type: ignore[attr-defined]
            data = response.read()
    except OSError as error:
        raise ResourceError(f"Download of {name} failed: {error}") from error
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise ResourceError(f"The downloaded {name} does not match the expected file.")

    directory = cache_directory()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    temporary = path + ".part"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)
    return path
