# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The progressive transfer of a capture through the bridge app.

:meth:`BridgeFetcher.prepare` has the app read the stopped acquisition into its cache
(``:BRIDge:SNAPshot``) and fetches an overview of every channel, so the capture can be shown at
once; :meth:`BridgeFetcher.start` then fetches the samples tile by tile in a thread, the tiles the
display shows first (``ProgressiveCapture.prioritize``). An interrupted transfer resumes where it
stopped (``resume_transfer``).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np

from . import codec, scpi
from ..base import CaptureTileArgs
from ...core.overview import Overview, ProgressiveCapture

if TYPE_CHECKING:  # pragma: no cover
    from .driver import RigolDhoDriver

log = logging.getLogger("openscilab.driver")

#: samples of one tile
TILE_SAMPLES = 1 << 20
#: blocks of the overview at most (its level is chosen so)
OVERVIEW_BLOCKS = 1 << 16
#: seconds the app may take to read the acquisition into its cache
SNAPSHOT_TIMEOUT_S = 600.0


def channel_list(numbers: list[int], analog: list[int]) -> str:
    items = (["D0-D15"] if numbers else []) + [f"CH{number + 1}" for number in analog]
    return ",".join(items)


class BridgeFetcher:
    def __init__(self, driver: "RigolDhoDriver", session, numbers: list[int], analog: list[int]) -> None:
        self.driver = driver
        self.session = session
        self.numbers = numbers
        self.analog = analog
        self.snapshot: Optional[str] = None
        self.meta: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def connection(self):
        return self.driver.connection

    def prepare(self) -> None:
        from ...core.sample_store import DiskAllocator

        connection = self.connection
        self.snapshot = connection.query(f":BRIDge:SNAPshot {channel_list(self.numbers, self.analog)}",
                                         timeout=SNAPSHOT_TIMEOUT_S).strip()
        if not self.snapshot or self.snapshot.startswith("ERR"):
            raise scpi.ScpiError(f"the bridge could not read the acquisition: {self.snapshot}")
        self.meta = scpi.parse_meta(connection.query(f":BRIDge:META? {self.snapshot}"))
        points = int(self.meta["points"])
        session = self.session
        session.frequency = int(round(float(self.meta.get("rate", session.frequency))))
        trigger = min(max(int(float(self.meta.get("trigger", 0))), 0), points)
        session.pre_trigger_samples, session.post_trigger_samples = trigger, points - trigger
        session.bursts = None
        level = 0
        while (points >> level) > OVERVIEW_BLOCKS:
            level += 1
        base = 1 << level
        progressive = ProgressiveCapture(points, TILE_SAMPLES)
        allocator = DiskAllocator()
        if self.numbers:
            pairs = np.frombuffer(connection.query_block(f":BRIDge:OVERview? {self.snapshot},D,{level}", timeout=60.0),
                                  dtype="<u2").reshape(-1, 2)
            for channel in session.capture_channels:
                bit = np.uint16(1 << channel.channel_number)
                mins = ((pairs[:, 0] & bit) != 0).astype(np.uint8)
                maxs = ((pairs[:, 1] & bit) != 0).astype(np.uint8)
                channel.samples = allocator.zeros(points, np.uint8)
                progressive.overviews[("d", channel.channel_number)] = Overview.from_blocks(mins, maxs, points, base)
        for channel in session.analog_channels:
            name = f"CH{channel.channel_number + 1}"
            pairs = np.frombuffer(connection.query_block(f":BRIDge:OVERview? {self.snapshot},{name},{level}",
                                                         timeout=60.0), dtype=np.uint8).reshape(-1, 2)
            channel.raw = allocator.zeros(points, np.int16)
            channel.length = points
            self.driver.set_analog(channel, None, float(self.meta.get(f"{name}.yinc", 1.0)),
                                   float(self.meta.get(f"{name}.yorig", 0.0)), float(self.meta.get(f"{name}.yref", 0.0)))
            progressive.overviews[("a", channel.channel_number)] = Overview.from_blocks(
                pairs[:, 0].astype(np.int16), pairs[:, 1].astype(np.int16), points, base)
        session.progressive = progressive

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="openscilab-dho-tiles", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, stop: threading.Event) -> None:
        session = self.session
        progressive: ProgressiveCapture = session.progressive
        progressive.interrupted = False
        try:
            while not stop.is_set():
                tile = progressive.next_tile()
                if tile is None:
                    return
                start, end = progressive.tile_range(tile)
                self._fetch(start, end)
                progressive.mark_loaded(tile)
                self.driver._raise_capture_tile(CaptureTileArgs(session, start, end - start, progressive.complete))
            progressive.interrupted = not progressive.complete
        except (scpi.ScpiError, ValueError) as error:
            log.debug("The DHO transfer stopped: %s", error)
            progressive.interrupted = True
            self.driver._raise_capture_tile(CaptureTileArgs(session, 0, 0, False, f"the transfer was interrupted: {error}"))

    def _fetch(self, start: int, end: int) -> None:
        count = end - start
        connection = self.connection
        if self.numbers:
            levels = codec.decode_digital(connection.query_block(
                f":BRIDge:TILE? {self.snapshot},D,{start},{count}", timeout=60.0))
            if len(levels) != count:
                raise ValueError(f"a tile of {len(levels)} instead of {count} samples")
            for channel in self.session.capture_channels:
                channel.samples[start:end] = (levels >> np.uint16(channel.channel_number)) & 1
        for channel in self.session.analog_channels:
            values = codec.decode_analog(connection.query_block(
                f":BRIDge:TILE? {self.snapshot},CH{channel.channel_number + 1},{start},{count}", timeout=60.0))
            if len(values) != count:
                raise ValueError(f"a tile of {len(values)} instead of {count} samples")
            channel.raw[start:end] = values


class BridgeCache:
    """The snapshot cache of the bridge app (``:BRIDge:CACHe``)."""

    def __init__(self, driver: "RigolDhoDriver") -> None:
        self.driver = driver

    def entries(self) -> list[tuple[str, float, int]]:
        text = self.driver.connection.query(":BRIDge:CACHe:LIST?").strip()
        result = []
        for item in text.split(";") if text else []:
            parts = item.split(",")
            if len(parts) >= 3:
                result.append((parts[0], float(parts[1]), int(float(parts[2]))))
        return result

    def delete(self, entry_id: Optional[str]) -> None:
        self._expect_ok(self.driver.connection.query(f":BRIDge:CACHe:DELete {entry_id or 'ALL'}"))

    def set_limit(self, size: int) -> None:
        self._expect_ok(self.driver.connection.query(f":BRIDge:CACHe:LIMit {int(size)}"))

    @staticmethod
    def _expect_ok(answer: str) -> None:
        if answer.strip() != "OK":
            raise scpi.ScpiError(f"the bridge refused: {answer}")
