# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Driver for the Rigol DHO900 oscilloscopes (DHO924S): 16 logic channels D0–D15, four analog
channels CH1–CH4 and the built-in generator.

A capture: thresholds → channels → memory depth and time base → trigger → ``:SINGle`` → the
trigger status until ``STOP`` → the data. With the bridge app on the instrument (port 5560,
``docs/protocols.md``) the data arrive progressively: the capture is complete at once with an
overview of every channel and the samples follow tile by tile (:mod:`.fetcher`). Without it the
driver reads ``:WAVeform:DATA?`` block by block from the instrument's own server (port 5555).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

from . import scpi
from .scpi import ScpiConnection, ScpiError, command
from ..base import (
    CAPABILITY_AFG,
    CAPABILITY_IMMEDIATE_TRIGGER,
    CAPABILITY_PATTERN_GROUPS,
    CAPABILITY_PROGRESSIVE,
    CAPABILITY_THRESHOLD,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    DeviceConnectionError,
    DeviceSection,
)
from ..models import CaptureSession, TriggerType

log = logging.getLogger("openscilab.driver")

DIGITAL = [f"D{index}" for index in range(16)]
ANALOG = [f"CH{index}" for index in range(1, 5)]
#: highest sample rate (Sa/s) and memory depth of the DHO924S (points), with analog channels
MAX_RATE = 625_000_000
DEPTH_DIGITAL = 31_250_000
DEPTH_ANALOG = {1: 25_000_000, 2: 25_000_000, 3: 10_000_000, 4: 10_000_000}
#: seconds between two looks at the trigger status
STATUS_INTERVAL_S = 0.05
#: seconds a capture waits for its trigger at most
TRIGGER_TIMEOUT_S = 3600.0


class RigolDhoDriver(AnalyzerDriverBase):
    """A DHO900 over the network, directly or through the bridge app."""

    def __init__(self, host: str, port: Optional[int] = None, address: str = "") -> None:
        super().__init__()
        self.host = host
        self._address = address or f"rigol:{host}"
        self.bridge_version: Optional[str] = None
        self.connection = self._connect(host, port)
        try:
            self.identity = self.connection.query(command("identify"))
        except ScpiError as error:
            self.connection.close()
            raise DeviceConnectionError(f"{host} does not answer: {error}") from error
        parts = [part.strip() for part in self.identity.split(",")]
        if len(parts) < 2 or "RIGOL" not in parts[0].upper():
            self.connection.close()
            raise DeviceConnectionError(f"{host} is no Rigol instrument ({self.identity!r})")
        self.model = parts[1]
        self.serial = parts[2] if len(parts) > 2 else ""
        self.firmware = parts[3] if len(parts) > 3 else ""
        self._capturing = False
        self._abort = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fetcher = None
        self._facets: Optional[list] = None

    def _connect(self, host: str, port: Optional[int]) -> ScpiConnection:
        """The bridge app when it answers (on ``port`` or 5560), else the instrument (5555)."""
        tried = []
        for candidate in ([port] if port else [scpi.BRIDGE_PORT, scpi.SCPI_PORT]):
            try:
                connection = ScpiConnection(host, candidate, timeout=5.0)
            except ScpiError as error:
                tried.append(str(error))
                continue
            try:
                answer = connection.query(":BRIDge:VERSion?", timeout=1.0)
                if answer.startswith("OPENSCILAB_BRIDGE"):
                    self.bridge_version = answer.split(",")[1] if "," in answer else "?"
                    return connection
            except ScpiError:
                # the instrument itself does not know the command: it answers nothing (an entry
                # in its error queue); a fresh connection leaves no half answer behind
                connection.close()
                connection = ScpiConnection(host, candidate, timeout=5.0)
                try:
                    connection.errors()
                except ScpiError:
                    pass
            return connection
        raise DeviceConnectionError("; ".join(tried) or f"{host} cannot be reached")

    # -------------------------------------------------------------- identity
    @property
    def device_version(self) -> Optional[str]:
        return f"Rigol {self.model} {self.firmware}".strip()

    @property
    def address(self) -> Optional[str]:
        return self._address

    @property
    def driver_id(self) -> str:
        return "rigol-" + self.model.lower()

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.OTHER

    @property
    def is_network(self) -> bool:
        return True

    @property
    def max_frequency(self) -> int:
        return MAX_RATE

    @property
    def min_frequency(self) -> int:
        return 1

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def channel_count(self) -> int:
        return len(DIGITAL)

    def channel_names(self) -> list[str]:
        return list(DIGITAL)

    @property
    def buffer_size(self) -> int:
        return DEPTH_DIGITAL

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    @property
    def uses_bridge(self) -> bool:
        return self.bridge_version is not None

    def capabilities(self) -> frozenset[str]:
        items = {CAPABILITY_IMMEDIATE_TRIGGER, CAPABILITY_THRESHOLD, f"{CAPABILITY_PATTERN_GROUPS}0-15",
                 f"ANALOG={len(ANALOG)}", CAPABILITY_AFG}
        if self.uses_bridge:
            items.add(CAPABILITY_PROGRESSIVE)
        return frozenset(items)

    def analog_channel_names(self) -> list[str]:
        return list(ANALOG)

    def describe(self) -> list[DeviceSection]:
        rows = [("Model", self.model), ("Serial number", self.serial or "-"), ("Firmware", self.firmware or "-"),
                ("Address", self.host),
                ("Transfer", f"bridge app {self.bridge_version}" if self.uses_bridge
                 else "directly from the instrument (slower; install the bridge app)")]
        return [("Rigol oscilloscope", rows)]

    def device_details(self) -> dict[str, str]:
        return {"Identity": self.identity, "Bridge": self.bridge_version or "-"}

    # ---------------------------------------------------------------- limits
    def memory_depth(self, digital: int, analog: int) -> Optional[int]:
        if analog:
            return DEPTH_ANALOG[min(max(analog, 1), 4)]
        return DEPTH_DIGITAL

    def get_limits(self, channels, acquisition_mode=None, *, to_disk=False, continuous=False) -> CaptureLimits:
        depth = DEPTH_DIGITAL
        return CaptureLimits(min_pre_samples=0, max_pre_samples=depth // 2, min_post_samples=1,
                             max_post_samples=depth)

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        return ((0, 16),)

    def edge_trigger_channels(self) -> list[int]:
        return list(range(16))

    # --------------------------------------------------------------- capture
    def start_capture(self, session: CaptureSession,
                      completed_handler: Optional[CaptureCompletedHandler] = None) -> CaptureError:
        if self._capturing:
            return CaptureError.BUSY
        numbers = session.channel_numbers
        analog = sorted({channel.channel_number for channel in session.analog_channels})
        if (not numbers and not analog) or any(not 0 <= n < 16 for n in numbers) \
                or any(not 0 <= n < 4 for n in analog) or session.loop_count:
            return CaptureError.BAD_PARAMS
        total = session.pre_trigger_samples + session.post_trigger_samples
        depth = self.memory_depth(len(numbers), len(analog)) or DEPTH_DIGITAL
        if total > depth or session.frequency <= 0 or session.frequency > MAX_RATE:
            return CaptureError.BAD_PARAMS
        if session.trigger_type not in (TriggerType.IMMEDIATE, TriggerType.EDGE, TriggerType.COMPLEX,
                                        TriggerType.FAST):
            return CaptureError.UNSUPPORTED
        try:
            self._configure(session, numbers, analog, total)
            self.connection.write(command("single"))
        except ScpiError as error:
            log.debug("Configuring the DHO failed: %s", error)
            return CaptureError.HARDWARE_ERROR
        self._capturing = True
        self._abort = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(session, numbers, analog, completed_handler,
                                                                self._abort), name="openscilab-dho", daemon=True)
        self._thread.start()
        return CaptureError.NONE

    def _configure(self, session: CaptureSession, numbers: list[int], analog: list[int], total: int) -> None:
        write = self.connection.write
        write(command("stop"))
        write(command("la_state", value="ON" if numbers else "OFF"))
        for index in range(16):
            write(command("la_channel", n=index, value="ON" if index in numbers else "OFF"))
        if session.threshold_voltage is not None:
            for pod in (1, 2):
                write(command("la_threshold", n=pod, value=f"{session.threshold_voltage:g}"))
        for index in range(4):
            write(command("channel", n=index + 1, value="ON" if index in analog else "OFF"))
        # the instrument has fixed memory depths: the smallest that holds the capture, over the time
        # that gives the rate asked for (the capture then has more samples than asked for)
        depth = next((value for value in scpi.MEMORY_DEPTHS if value >= total), scpi.MEMORY_DEPTHS[-1])
        write(command("memory_depth", value=depth))
        write(command("timebase_scale", value=f"{depth / session.frequency / scpi.DIVISIONS:.6e}"))
        # the trigger is at the centre of the screen; the offset moves it to the pre-trigger samples
        write(command("timebase_offset", value=f"{(depth / 2 - session.pre_trigger_samples) / session.frequency:.6e}"))
        kind = session.trigger_type
        if kind == TriggerType.IMMEDIATE:
            write(command("trigger_sweep", value="AUTO"))
        elif kind == TriggerType.EDGE:
            write(command("trigger_mode", value="EDGE"))
            write(command("edge_source", value=f"D{session.trigger_channel}"))
            write(command("edge_slope", value="NEGative" if session.trigger_inverted else "POSitive"))
            write(command("trigger_sweep", value="NORMal"))
        else:
            levels = ["X"] * 20  # CH1–CH4, D0–D15
            for offset in range(max(session.trigger_bit_count, 1)):
                channel = session.trigger_channel + offset
                if session.trigger_mask is not None and not (session.trigger_mask >> offset) & 1:
                    continue  # (don't care: X)
                if 0 <= channel < 16:
                    levels[4 + channel] = "H" if (session.trigger_pattern >> offset) & 1 else "L"
            write(command("trigger_mode", value="PATTern"))
            write(command("pattern", value=",".join(levels)))
            write(command("trigger_sweep", value="NORMal"))
        errors = self.connection.errors()
        if errors:
            log.warning("The DHO reported while configuring: %s", "; ".join(errors))

    def _wait_for_trigger(self, abort: threading.Event) -> bool:
        deadline = time.monotonic() + TRIGGER_TIMEOUT_S
        while not abort.wait(STATUS_INTERVAL_S):
            if self.connection.query(command("status")).upper().startswith("STOP"):
                return True
            if time.monotonic() > deadline:
                raise ScpiError("no trigger came")
        return False

    def _run(self, session, numbers, analog, handler, abort: threading.Event) -> None:
        try:
            if not self._wait_for_trigger(abort):
                return  # stopped
            rate = float(self.connection.query(command("sample_rate?")))
            session.frequency = int(round(rate))
            if self.uses_bridge:
                from .fetcher import BridgeFetcher

                self._fetcher = BridgeFetcher(self, session, numbers, analog)
                self._fetcher.prepare()
                self._capturing = False
                self._raise_capture_completed(CaptureCompletedArgs(success=True, session=session), handler)
                self._fetcher.start()
                return
            self._read_direct(session, numbers, analog, abort)
            if abort.is_set():
                return
            self._capturing = False
            self._raise_capture_completed(CaptureCompletedArgs(success=True, session=session), handler)
        except (ScpiError, ValueError) as error:
            if abort.is_set():
                return
            self._capturing = False
            log.debug("DHO capture failed: %s", error)
            self._raise_capture_completed(CaptureCompletedArgs(success=False, session=session, error=str(error)),
                                          handler)

    def _read_source(self, source: str, abort: threading.Event) -> tuple[np.ndarray, dict[str, float]]:
        connection = self.connection
        connection.write(command("wave_source", value=source))
        connection.write(command("wave_mode"))
        connection.write(command("wave_format"))
        preamble = scpi.parse_preamble(connection.query(command("wave_preamble?")))
        points = int(preamble["points"])
        data = np.zeros(points, dtype=np.uint8)
        for start in range(0, points, scpi.MAX_POINTS_PER_READ):
            if abort.is_set():
                break
            stop = min(start + scpi.MAX_POINTS_PER_READ, points)
            # STARt never behind STOP: STOP first when the window moves on
            connection.write(command("wave_stop", value=stop))
            connection.write(command("wave_start", value=start + 1))
            connection.write(command("wave_stop", value=stop))
            block = connection.query_block(command("wave_data?"), timeout=30.0)
            data[start:start + len(block)] = np.frombuffer(block, dtype=np.uint8)
        return data, preamble

    def _read_direct(self, session, numbers, analog, abort) -> None:
        preamble: dict[str, float] = {}
        for channel in session.capture_channels:
            values, preamble = self._read_source(f"D{channel.channel_number}", abort)
            channel.samples = (values != 0).astype(np.uint8)
        for channel in session.analog_channels:
            values, preamble = self._read_source(f"CHANnel{channel.channel_number + 1}", abort)
            self.set_analog(channel, values, preamble["yinc"], preamble["yorig"], preamble["yref"])
        if preamble:
            points = int(preamble["points"])
            trigger = int(round(preamble["xref"] - preamble["xorig"] / preamble["xinc"])) if preamble["xinc"] else 0
            session.pre_trigger_samples = min(max(trigger, 0), points)
            session.post_trigger_samples = points - session.pre_trigger_samples
        session.bursts = None

    @staticmethod
    def set_analog(channel, raw: Optional[np.ndarray], yinc: float, yorig: float, yref: float) -> None:
        """Raw 8 bit values; volts = (raw − yorig − yref) · yinc."""
        if raw is not None:
            channel.raw = np.asarray(raw).astype(np.int16)
            channel.length = None
        channel.scale = float(yinc)
        channel.offset = -(float(yorig) + float(yref)) * float(yinc)
        channel.unit = "V"
        channel.rate = None
        channel.channel_name = channel.channel_name or ANALOG[channel.channel_number]

    def stop_capture(self) -> bool:
        if not self._capturing:
            return False
        self._abort.set()
        self._capturing = False
        try:
            self.connection.write(command("stop"))
        except ScpiError:
            pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)
        return True

    # ------------------------------------------------- progressive transfer
    def resume_transfer(self, session: CaptureSession) -> bool:
        fetcher = self._fetcher
        if fetcher is None or fetcher.session is not session:
            return False
        fetcher.start()
        return True

    def stop_transfer(self) -> None:
        if self._fetcher is not None:
            self._fetcher.stop()

    # ---------------------------------------------------------------- facets
    def instrument_facets(self) -> list:
        if self._facets is None:
            from .generator import DhoCache, DhoGenerator

            self._facets = [DhoGenerator(self)] + ([DhoCache(self)] if self.uses_bridge else [])
        return list(self._facets)

    def dispose(self) -> None:
        if self._capturing:
            self.stop_capture()
        if self._fetcher is not None:
            self._fetcher.stop()
        self.connection.close()
        super().dispose()


def open_network(text: str) -> RigolDhoDriver:
    """``<host>`` or ``<host>:<port>``."""
    host, _, port = text.strip().partition(":")
    if not host:
        raise DeviceConnectionError("No address of the oscilloscope given.")
    try:
        return RigolDhoDriver(host, int(port) if port else None, address=f"rigol:{text.strip()}")
    except ScpiError as error:
        raise DeviceConnectionError(str(error)) from error
