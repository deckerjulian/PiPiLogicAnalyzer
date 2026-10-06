# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Simulator profiles: what a simulated instrument is (pins, channels, rates, depth, limits).

Profiles are JSON files in ``examples/sim/`` (``uno``, ``uno_r4``, ``pico``, ``daq``, ``dho924s``) and in the
folder ``sim`` of the settings directory (your own); ``free`` is built in, so the simulator works
without any file. The keys are described in ``docs/simulator.md``.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from typing import Optional

from ...core.settings import settings_directory

PROJECT_DIRECTORY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PROFILE_DIRECTORIES = [os.path.join(PROJECT_DIRECTORY, "examples", "sim")]


def _counter_sources() -> dict:
    sources = {f"D{bit}": {"type": "counter", "frequency": "1 MHz", "bit": bit} for bit in range(8)}
    sources["D8"] = {"type": "clock", "frequency": "1 MHz", "phase": 0.5}
    sources["D9"] = {"type": "uart", "text": "openSciLab\n", "baud": 115200}
    sources["D10"] = {"type": "spi", "line": "clk", "frequency": "1 MHz"}
    sources["D11"] = {"type": "spi", "line": "mosi", "frequency": "1 MHz"}
    sources["D12"] = {"type": "spi", "line": "cs", "frequency": "1 MHz"}
    sources["D13"] = {"type": "i2c", "line": "scl", "frequency": "1 MHz"}
    sources["D14"] = {"type": "i2c", "line": "sda", "frequency": "1 MHz"}
    sources["D15"] = {"type": "square", "frequency": "1 kHz"}
    return sources


#: The built-in profile: a 16 channel logic analyzer with test signals.
BUILTIN = {
    "free": {
        "name": "free",
        "title": "Simulation: free",
        "description": "16 digital channels with test signals: a counter on D0-D7 (1 MHz, its clock "
                       "on D8), UART on D9, SPI on D10-D12, I²C on D13-D14, 1 kHz on D15. A pattern "
                       "generator drives D0-D7 while it runs (looped back to the inputs); trigger "
                       "input TRIG IN on D15.",
        "digital": [f"D{index}" for index in range(16)],
        "analog": [],
        "max_rate": 100_000_000,
        "memory_depth": 4_000_000,
        "stream_bandwidth": 12_000_000,
        "latency": 0.0,
        "logic_level": 3.3,
        "capabilities": ["IMMEDIATE_TRIGGER", "CONTINUOUS_STREAM", "SIMULATION", "STATE_MODE", "STREAM_STATE",
                         "PATTERN_GEN=25000000,8"],
        "clock_pins": ["D8", "D10", "D13", "D15"],
        "outputs": [{"name": "PATTERN", "kind": "pattern", "pins": [f"D{index}" for index in range(8)],
                     "max_rate": 25_000_000, "max_points": 1 << 20}],
        "trigger_inputs": {"TRIG IN": "D15"},
        "circuit": {"sources": _counter_sources()},
    },
}


def profile_directories() -> list[str]:
    directories = list(PROFILE_DIRECTORIES)
    try:
        directories.append(os.path.join(settings_directory(), "sim"))
    except OSError:  # pragma: no cover - no settings directory
        pass
    return directories


_files_cache: tuple[tuple, dict[str, str]] = ((), {})
_profile_cache: dict[str, tuple[float, dict]] = {}


def _files() -> dict[str, str]:
    """Profile files by name. The folders are listed again only when one of them changed
    (their modification time): the device list and the palette ask often."""
    global _files_cache
    directories = [directory for directory in profile_directories() if os.path.isdir(directory)]
    stamp = tuple((directory, os.path.getmtime(directory)) for directory in directories)
    if stamp == _files_cache[0]:
        return _files_cache[1]
    found: dict[str, str] = {}
    for directory in directories:
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json"):
                found.setdefault(name[:-5], os.path.join(directory, name))
    _files_cache = (stamp, found)
    return found


def available_profiles() -> list[str]:
    return sorted(set(BUILTIN) | set(_files()))


def load_profile(name: str) -> dict:
    """The profile ``name`` (a file overrides the built-in one of the same name); a copy the
    caller may change. Files are read again only when they changed."""
    path = _files().get(name)
    if path is not None:
        stamp = os.path.getmtime(path)
        cached = _profile_cache.get(path)
        if cached is None or cached[0] != stamp:
            with open(path, encoding="utf-8") as handle:
                profile = json.load(handle)
            profile.setdefault("name", name)
            cached = _profile_cache[path] = (stamp, _with_defaults(profile))
        return copy.deepcopy(cached[1])
    if name in BUILTIN:
        return _with_defaults(copy.deepcopy(BUILTIN[name]))
    raise ValueError(f"unknown simulator profile {name!r} (available: {', '.join(available_profiles())})")


def profile_title(name: str) -> str:
    """The title of the profile ``name`` (cheap: from the cache), the name when it cannot be read."""
    try:
        return str(load_profile(name)["title"]) if name in BUILTIN or name in _files() else name
    except (OSError, ValueError, KeyError):
        return name


# ------------------------------------------------------------- addresses
#: boards of a simulated multi device
MAX_BOARDS = 5
#: what a multi device set can do of the capabilities of its boards (as the real one, ``pico/multi.py``:
#: it captures into the boards' buffers - no stream, no state mode, no outputs)
_MULTI_BOARD_CAPABILITIES = ("IMMEDIATE_TRIGGER",)


@dataclass(frozen=True)
class SimAddress:
    """``sim:<profile>[*<boards>][#<instance>]``: ``sim:uno``, ``sim:pico*2`` (a multi device of
    two simulated Picos), ``sim:uno#2`` (a second Uno beside the first)."""

    profile: str
    boards: int = 1
    instance: int = 1

    @staticmethod
    def parse(text: str) -> "SimAddress":
        text = text.strip()
        if text.startswith("sim:"):
            text = text[4:]
        text, _, instance = text.partition("#")
        name, _, boards = text.partition("*")
        try:
            address = SimAddress(name.strip() or "free", int(boards) if boards else 1, int(instance) if instance else 1)
        except ValueError:
            raise ValueError(f"{text!r} is not a simulator (sim:<profile>[*<boards>][#<number>])") from None
        if not 1 <= address.boards <= MAX_BOARDS:
            raise ValueError(f"a simulated multi device has 2 to {MAX_BOARDS} boards")
        if address.instance < 1:
            raise ValueError(f"#{address.instance}: the instances of a simulator count from 1 (sim:uno#2)")
        return address

    @property
    def spec(self) -> str:
        """Profile and boards: what kind of device it is (``pico*2``)."""
        return self.profile + (f"*{self.boards}" if self.boards > 1 else "")

    def __str__(self) -> str:
        return f"sim:{self.spec}" + (f"#{self.instance}" if self.instance > 1 else "")

    def title(self, profile_title: str) -> str:
        text = profile_title + (f" × {self.boards}" if self.boards > 1 else "")
        return text + (f" ({self.instance})" if self.instance > 1 else "")


def free_address(spec: str, taken) -> SimAddress:
    """The first address of ``spec`` (``uno``, ``pico*2``) that is not in ``taken`` (addresses in use)."""
    address = SimAddress.parse(spec)
    used = set(taken)
    instance = 1
    while str(SimAddress(address.profile, address.boards, instance)) in used:
        instance += 1
    return SimAddress(address.profile, address.boards, instance)


def multi_profile(profile: dict, boards: int) -> dict:
    """The profile of a multi device set of ``boards`` boards of ``profile``: their digital
    channels one board after the other (``B2.GP5``), captured together. As a real set it only
    captures into the buffers: no stream, state mode, GPIO, generator or analog inputs."""
    if boards <= 1:
        return profile
    names = list(profile["digital"])
    if not names:
        raise ValueError(f"{profile['title']} has no digital channels for a multi device")

    def net(board: int, name: str) -> str:
        return f"B{board + 1}.{name}"

    sources = (profile.get("circuit") or {}).get("sources") or {}
    table = profile.get("memory_depth_digital") or {}
    depth = min([int(value) for value in table.values()] + [int(profile["memory_depth"])])
    result = {
        "name": f"{profile['name']}*{boards}",
        "title": f"{profile['title']} × {boards}",
        "description": f"A multi device set of {boards} × {profile['title']}: {len(names) * boards} channels "
                       f"captured together (board 1: channels 1–{len(names)}, board 2 from {len(names) + 1}, …).",
        "digital": [net(board, name) for board in range(boards) for name in names],
        "analog": [],
        "boards": boards,
        "channels_per_board": len(names),
        "max_rate": profile["max_rate"],
        "memory_depth": depth,
        "stream_bandwidth": 0,
        "latency": profile["latency"],
        "logic_level": profile["logic_level"],
        "capabilities": [item for item in profile.get("capabilities", []) if item in _MULTI_BOARD_CAPABILITIES],
        "circuit": {"sources": {net(board, name): copy.deepcopy(source) for board in range(boards)
                                for name, source in sources.items() if name in names}},
    }
    for key in ("progressive", "provisional"):
        if key in profile:
            result[key] = profile[key]
    return _with_defaults(result)


def _with_defaults(profile: dict) -> dict:
    profile.setdefault("title", f"Simulation: {profile['name']}")
    profile.setdefault("digital", [])
    profile.setdefault("analog", [])
    profile.setdefault("max_rate", 1_000_000)
    profile.setdefault("memory_depth", 100_000)
    profile.setdefault("stream_bandwidth", 1_000_000)
    profile.setdefault("latency", 0.0)
    profile.setdefault("logic_level", 3.3)
    profile.setdefault("capabilities", [])
    profile.setdefault("circuit", {})
    return profile


def profile_path(name: str) -> Optional[str]:
    return _files().get(name)
