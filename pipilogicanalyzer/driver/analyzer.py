# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Driver for a single PiPiLogicAnalyzer device (serial or network).

Port of ``SharedDriver/PiPiLogicAnalyzerDriver.cs``.

Differences with the original implementation:

* the capture payload is read in one block and decoded with ``numpy`` instead of
  one ``BinaryReader`` call per sample (orders of magnitude faster for the
  1M-sample captures the device supports);
* the burst timestamp block is read using the length reported by the device
  instead of a length derived from the requested loop count -- the firmware only
  sends timestamps when it reports more than one, so the original code could
  block forever waiting for data that was never sent;
* errors are reported to the completion handler with a message instead of being
  swallowed;
* the request layout follows the firmware version, so V6_0 devices (24 channels,
  8 bit loop count) keep working next to V6_5 devices (32 channels, up to 65534
  bursts) -- the V6_5 original only talks to V6_5 firmware.

Driver activity is logged through :mod:`logging` (``pipilogicanalyzer.driver``),
the counterpart of the ``DEBUG_MODE`` log added in V6_5.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional, Sequence

import numpy as np

from . import protocol
from .base import (
    COMPLEX_TRIGGER_DELAY,
    DEFAULT_PATTERN_GROUPS,
    EDGE_OUT_TRIGGER_DELAY,
    FAST_TRIGGER_DELAY,
    MAX_MEASURED_LOOP_COUNT,
    MIN_MEASURED_POST_SAMPLES,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CAPABILITY_DEVICE_INFO,
    CAPABILITY_EDGE_TRIGGER_OUT,
    CAPABILITY_SELF_TEST,
    CAPABILITY_SIMULATION,
    CaptureMode,
    DeviceConnectionError,
    SelfTestResult,
    UnsupportedFeatureError,
    parse_pattern_groups,
    parse_self_test_line,
    parse_version,
    pattern_fits,
    pattern_max_bits,
    trigger_delay_samples,
)
from ..core.simulation import SimulationPattern
from .models import BurstInfo, CaptureSession, TriggerType
from .transport import NetworkTransport, SerialTransport, Transport, TransportError

_ADDRESS_PORT_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+):(\d+)$")
_CHANNELS_RE = re.compile(r"^CHANNELS:(\d+)$")
_BUFFER_RE = re.compile(r"^BUFFER:(\d+)$")
_FREQ_RE = re.compile(r"^FREQ:(\d+)$")
_BLAST_RE = re.compile(r"^BLASTFREQ:(\d+)$")

#: Systick period used when the device reports no blast frequency.  The systick
#: runs at the CPU clock, which is the blast frequency (200MHz -> 5ns on RP2040).
NS_PER_TICK = 5.0

log = logging.getLogger("pipilogicanalyzer.driver")


def unpack_channel_samples(raw_samples: np.ndarray, channel_index: int) -> np.ndarray:
    """Extract the samples of one channel from the packed capture words."""
    return ((raw_samples >> np.uint32(channel_index)) & np.uint32(1)).astype(np.uint8)


class PiPiLogicAnalyzerDriver(AnalyzerDriverBase):
    """Talks to one device over USB CDC or over TCP."""

    def __init__(self, connection_string: str, connect_timeout: float = 10.0) -> None:
        super().__init__()
        if not connection_string:
            raise ValueError("A connection string is required")

        self._capturing = False
        self._abort = threading.Event()
        self._lock = threading.RLock()
        self._capture_thread: Optional[threading.Thread] = None

        self._version: Optional[str] = None
        self._channel_count = 0
        self._max_frequency = 0
        self._blast_frequency = 0
        self._buffer_size = 0
        self._is_network = False
        self._request_layout = protocol.LAYOUT_V6_0
        self._capabilities: Optional[frozenset[str]] = None
        self.connection_string = connection_string

        match = _ADDRESS_PORT_RE.match(connection_string)
        if match:
            self._is_network = True
            self._transport: Transport = NetworkTransport(
                match.group(1), int(match.group(2)), timeout=connect_timeout
            )
        elif ":" in connection_string:
            raise ValueError(f"Invalid address/port: {connection_string}")
        else:
            self._transport = SerialTransport(connection_string, timeout=connect_timeout)

        log.debug("Opening %s device %s", "network" if self._is_network else "serial", connection_string)
        try:
            self._query_device_info()
        except Exception as error:
            log.debug("Error initializing device: %s", error)
            self.dispose()
            raise

    # --------------------------------------------------------------- identity
    def _query_device_info(self) -> None:
        self._transport.write(protocol.command_packet(protocol.CMD_GET_ID))

        self._version = self._transport.read_line(timeout=10.0)
        log.debug("Device version: %s", self._version)
        version = parse_version(self._version)
        if not version.is_valid:
            raise DeviceConnectionError(
                f"Invalid device version {self._version}, minimum supported version: "
                f"V6_0"
            )
        self._request_layout = protocol.layout_for_version(version.major, version.minor)

        self._max_frequency = self._read_int_response(_FREQ_RE, "frequency")
        self._blast_frequency = self._read_int_response(_BLAST_RE, "blast frequency")
        self._buffer_size = self._read_int_response(_BUFFER_RE, "buffer size")
        self._channel_count = self._read_int_response(_CHANNELS_RE, "channel count")
        log.debug(
            "Device initialized: freq=%d blast=%d buffer=%d channels=%d layout=%d bytes",
            self._max_frequency,
            self._blast_frequency,
            self._buffer_size,
            self._channel_count,
            self._request_layout.size,
        )

    def _read_int_response(self, pattern: re.Pattern[str], description: str) -> int:
        line = self._transport.read_line(timeout=10.0)
        match = pattern.match(line or "")
        if not match:
            raise DeviceConnectionError(f"Invalid device {description} response: {line!r}")
        return int(match.group(1))

    # ------------------------------------------------------------- properties
    @property
    def device_version(self) -> Optional[str]:
        return self._version

    @property
    def channel_count(self) -> int:
        return self._channel_count

    @property
    def max_frequency(self) -> int:
        return self._max_frequency

    @property
    def blast_frequency(self) -> int:
        return self._blast_frequency

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @property
    def max_loop_count(self) -> int:
        return self._request_layout.max_loop_count

    @property
    def request_layout(self) -> protocol.RequestLayout:
        return self._request_layout

    @property
    def is_network(self) -> bool:
        return self._is_network

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.NETWORK if self._is_network else AnalyzerDriverType.SERIAL

    # ---------------------------------------------------------------- capture
    def start_capture(
        self, session: CaptureSession, completed_handler: Optional[CaptureCompletedHandler] = None
    ) -> CaptureError:
        with self._lock:
            if self._capturing:
                return CaptureError.BUSY
            if not session.capture_channels:
                return CaptureError.BAD_PARAMS

            if (
                session.trigger_type == TriggerType.SIMULATION
                and CAPABILITY_SIMULATION not in self.capabilities()
            ):
                return CaptureError.UNSUPPORTED

            requested_samples = session.pre_trigger_samples + session.post_trigger_samples * (
                session.loop_count + 1
            )
            if not self.validate_settings(session, requested_samples):
                log.debug("Capture rejected: invalid settings")
                return CaptureError.BAD_PARAMS

            mode = self.get_capture_mode(session.channel_numbers)
            request = self.compose_request(session, mode)

            try:
                self._transport.reset_input()
                self._transport.write(
                    protocol.command_packet(
                        protocol.CMD_START_CAPTURE, request.pack(self._request_layout)
                    )
                )
                result = self._transport.read_line(timeout=10.0)
            except (TransportError, OSError) as error:
                log.debug("Error starting capture: %s", error)
                return CaptureError.HARDWARE_ERROR

            if result != "CAPTURE_STARTED":
                log.debug("Error starting capture, device response: %r", result)
                return CaptureError.HARDWARE_ERROR
            log.debug("Capture started (%d samples, mode %s)", requested_samples, mode.name)

            self._capturing = True
            self._abort.clear()
            self._capture_thread = threading.Thread(
                target=self._read_capture,
                args=(session, mode, completed_handler),
                name="pipilogicanalyzer-capture",
                daemon=True,
            )
            self._capture_thread.start()
            return CaptureError.NONE

    def _read_capture(
        self,
        session: CaptureSession,
        mode: CaptureMode,
        completed_handler: Optional[CaptureCompletedHandler],
    ) -> None:
        try:
            length_bytes = self._transport.read_exactly(4)
            length = int(np.frombuffer(length_bytes, dtype="<u4")[0])

            payload = self._transport.read_exactly(length * mode.bytes_per_sample)
            dtype = {1: "<u1", 2: "<u2", 4: "<u4"}[mode.bytes_per_sample]
            raw_samples = np.frombuffer(payload, dtype=dtype).astype(np.uint32)

            timestamps = self._read_timestamps()
            session.bursts = self._compute_bursts(session, timestamps)

            for index, channel in enumerate(session.capture_channels):
                channel.samples = unpack_channel_samples(raw_samples, index)

            self._capturing = False
            log.debug("Capture read complete (%d samples)", raw_samples.size)
            self._raise_capture_completed(
                CaptureCompletedArgs(success=True, session=session), completed_handler
            )
        except Exception as error:  # noqa: BLE001 - reported to the UI
            if self._abort.is_set():
                # The user aborted the capture; the truncated read is expected.
                return
            log.debug("Error reading capture: %s", error)
            self._capturing = False
            self._raise_capture_completed(
                CaptureCompletedArgs(success=False, session=session, error=str(error)),
                completed_handler,
            )

    def _read_timestamps(self) -> np.ndarray:
        stamp_length = self._transport.read_exactly(1)[0]
        if stamp_length <= 1:
            # The firmware only transmits the timestamp block when it holds more
            # than one entry.
            return np.empty(0, dtype=np.uint64)
        raw = self._transport.read_exactly(stamp_length * 4)
        return np.frombuffer(raw, dtype="<u4").astype(np.uint64)

    def _compute_bursts(
        self, session: CaptureSession, timestamps: np.ndarray
    ) -> Optional[list[BurstInfo]]:
        """Convert the device timestamps into per burst gap information.

        The systick counter counts downwards, so the lower 24 bits are inverted
        first.  Afterwards the jitter introduced by the device (up to 1us) is
        compensated, exactly like the original implementation did.  As in V6_5
        the tick length is derived from the blast frequency (the CPU clock), so
        RP2350 based boards get correct gaps as well.
        """
        if timestamps.size == 0:
            return None

        stamps = timestamps.astype(np.int64).copy()
        stamps = (stamps & 0xFF000000) | (0x00FFFFFF - (stamps & 0x00FFFFFF))

        ns_per_tick = (
            1_000_000_000.0 / self.blast_frequency if self.blast_frequency > 0 else NS_PER_TICK
        )
        ns_per_sample = 1_000_000_000.0 / session.frequency
        ticks_per_sample = ns_per_sample / ns_per_tick
        ticks_per_burst = (ns_per_sample * session.post_trigger_samples) / ns_per_tick

        for index in range(1, len(stamps)):
            top = stamps[index] + (1 << 32) if stamps[index] < stamps[index - 1] else stamps[index]
            if top - stamps[index - 1] <= ticks_per_burst:
                correction = int(ticks_per_burst - (top - stamps[index - 1]) + ticks_per_sample * 2)
                stamps[index:] += correction

        delays: list[float] = []
        for index in range(2, len(stamps)):
            top = stamps[index] + (1 << 32) if stamps[index] < stamps[index - 1] else stamps[index]
            delays.append(float((top - stamps[index - 1]) - ticks_per_burst) * ns_per_tick)

        bursts: list[BurstInfo] = []
        for index in range(1, len(stamps)):
            start = session.pre_trigger_samples + session.post_trigger_samples * (index - 1)
            end = session.pre_trigger_samples + session.post_trigger_samples * index
            if index == 1:
                bursts.append(BurstInfo(burst_sample_start=session.pre_trigger_samples,
                                        burst_sample_end=end,
                                        burst_sample_gap=0,
                                        burst_time_gap=0))
            else:
                delay = max(delays[index - 2], 0.0)
                bursts.append(
                    BurstInfo(
                        burst_sample_start=start,
                        burst_sample_end=end,
                        burst_sample_gap=int(delay / ns_per_sample),
                        burst_time_gap=int(delay),
                    )
                )
        return bursts

    def compose_request(self, session: CaptureSession, mode: CaptureMode) -> protocol.CaptureRequest:
        channels = session.channel_numbers
        if session.trigger_type == TriggerType.SIMULATION:
            return protocol.CaptureRequest(
                trigger_type=int(TriggerType.SIMULATION),
                trigger_value=session.trigger_pattern,
                channels=channels,
                channel_count=len(channels),
                frequency=session.frequency,
                pre_samples=session.pre_trigger_samples,
                post_samples=session.post_trigger_samples,
                capture_mode=int(mode),
            )

        if session.trigger_type in (TriggerType.EDGE, TriggerType.BLAST):
            return protocol.CaptureRequest(
                trigger_type=int(session.trigger_type),
                trigger=session.trigger_channel,
                inverted_or_count=1 if session.trigger_inverted else 0,
                channels=channels,
                channel_count=len(channels),
                frequency=session.frequency,
                pre_samples=session.pre_trigger_samples,
                post_samples=session.post_trigger_samples,
                loop_count=session.loop_count,
                measure=1 if session.measure_bursts else 0,
                capture_mode=int(mode),
            )

        offset = self.trigger_offset(session)
        if session.trigger_type == TriggerType.EDGE_OUT:
            # Started through the trigger line like a pattern trigger: the same delay compensation.
            return protocol.CaptureRequest(
                trigger_type=int(TriggerType.EDGE_OUT),
                trigger=session.trigger_channel,
                inverted_or_count=1 if session.trigger_inverted else 0,
                channels=channels,
                channel_count=len(channels),
                frequency=session.frequency,
                pre_samples=session.pre_trigger_samples + offset,
                post_samples=session.post_trigger_samples - offset,
                capture_mode=int(mode),
            )

        return protocol.CaptureRequest(
            trigger_type=int(session.trigger_type),
            trigger=session.trigger_channel,
            inverted_or_count=session.trigger_bit_count,
            trigger_value=session.trigger_pattern,
            channels=channels,
            channel_count=len(channels),
            frequency=session.frequency,
            pre_samples=session.pre_trigger_samples + offset,
            post_samples=session.post_trigger_samples - offset,
            capture_mode=int(mode),
        )

    def trigger_offset(self, session: CaptureSession) -> int:
        """Samples the pattern trigger lags behind, compensated in the request."""
        delay = {
            TriggerType.FAST: FAST_TRIGGER_DELAY,
            TriggerType.EDGE_OUT: EDGE_OUT_TRIGGER_DELAY,
        }.get(session.trigger_type, COMPLEX_TRIGGER_DELAY)
        return trigger_delay_samples(delay, self.max_frequency, session.frequency)

    # ------------------------------------------------------------- validation
    def validate_settings(self, session: CaptureSession, requested_samples: int) -> bool:
        channels: Sequence[int] = session.channel_numbers
        if not channels:
            return False

        limits = self.get_limits(channels)

        if min(channels) < 0 or max(channels) > self.channel_count - 1:
            return False

        if session.trigger_type == TriggerType.EDGE:
            measured = session.measure_bursts and session.loop_count > 0
            return (
                0 <= session.trigger_channel <= self.channel_count  # channel_count == ext trigger
                and limits.min_pre_samples <= session.pre_trigger_samples <= limits.max_pre_samples
                and limits.min_post_samples <= session.post_trigger_samples <= limits.max_post_samples
                and requested_samples <= limits.max_total_samples
                and self.min_frequency <= session.frequency <= self.max_frequency
                and 0 <= session.loop_count <= self.max_loop_count
                and not (measured and session.loop_count > MAX_MEASURED_LOOP_COUNT)
                and not (measured and session.post_trigger_samples < MIN_MEASURED_POST_SAMPLES)
            )

        if session.trigger_type == TriggerType.EDGE_OUT:
            return (
                CAPABILITY_EDGE_TRIGGER_OUT in self.capabilities()
                and 0 <= session.trigger_channel < self.channel_count
                and session.loop_count == 0
                and limits.min_pre_samples <= session.pre_trigger_samples <= limits.max_pre_samples
                and limits.min_post_samples <= session.post_trigger_samples <= limits.max_post_samples
                and requested_samples <= limits.max_total_samples
                and self.min_frequency <= session.frequency <= self.max_frequency
                # The trigger delay is compensated with post-trigger samples.
                and session.post_trigger_samples > self.trigger_offset(session)
            )

        if session.trigger_type == TriggerType.BLAST:
            return (
                0 <= session.trigger_channel <= self.channel_count
                and session.pre_trigger_samples == 0
                and limits.min_post_samples <= session.post_trigger_samples <= limits.max_total_samples
                and requested_samples <= limits.max_total_samples
                and session.frequency == self.blast_frequency
                and session.loop_count == 0
            )

        if session.trigger_type == TriggerType.SIMULATION:
            return (
                0 <= session.trigger_pattern < len(SimulationPattern)
                and 0 <= session.pre_trigger_samples <= limits.max_pre_samples
                and limits.min_post_samples <= session.post_trigger_samples <= limits.max_post_samples
                and requested_samples <= limits.max_total_samples
                and 1 <= session.frequency <= self.max_frequency
                and session.loop_count == 0
            )

        return (
            1 <= session.trigger_bit_count <= pattern_max_bits(session.trigger_type)
            and pattern_fits(self.pattern_trigger_groups(), session.trigger_channel, session.trigger_bit_count)
            and limits.min_pre_samples <= session.pre_trigger_samples <= limits.max_pre_samples
            and limits.min_post_samples <= session.post_trigger_samples <= limits.max_post_samples
            and requested_samples <= limits.max_total_samples
            and self.min_frequency <= session.frequency <= self.max_frequency
            and session.post_trigger_samples > self.trigger_offset(session)
        )

    # ------------------------------------------------------------------- stop
    def stop_capture(self) -> bool:
        if not self._capturing:
            return False

        self._capturing = False
        self._abort.set()

        try:
            self._transport.write(bytes([protocol.CMD_ABORT_CAPTURE]))
        except Exception:  # pragma: no cover - device may already be gone
            pass

        try:
            # Reconnecting is the only reliable way to resynchronise the stream
            # after aborting a capture in progress.
            self._transport.reopen()
        except Exception:  # pragma: no cover - device may already be gone
            pass

        return True

    # ------------------------------------------------------------ board test
    def capabilities(self) -> frozenset[str]:
        """Optional functions of the firmware, queried once and cached.

        Firmware without the extension (the original 6.5 or older) answers
        ``ERR_UNKNOWN_MSG``, which yields an empty set.
        """
        if self._capabilities is not None:
            return self._capabilities
        if self._capturing:
            return frozenset()

        with self._lock:
            try:
                self._transport.reset_input()
                self._transport.write(protocol.command_packet(protocol.CMD_CAPABILITIES))
                line = self._transport.read_line(timeout=5.0) or ""
            except (TransportError, OSError) as error:
                log.debug("Capability query failed: %s", error)
                return frozenset()

        if line.startswith("CAPS:"):
            self._capabilities = frozenset(
                item.strip() for item in line[len("CAPS:"):].split(",") if item.strip()
            )
        else:
            self._capabilities = frozenset()
        log.debug("Device capabilities: %s", sorted(self._capabilities))
        return self._capabilities

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        """Channel groups reported by the firmware; channels 1 to 16 for firmware without the report."""
        groups = parse_pattern_groups(self.capabilities())
        if groups is None:
            return DEFAULT_PATTERN_GROUPS
        return tuple(
            (first, min(count, self.channel_count - first))
            for first, count in groups
            if first < self.channel_count
        )

    def device_details(self) -> dict[str, str]:
        """``INFO:<key>:<value>`` lines of the firmware (chip, unique ID, SDK, ...)."""
        if CAPABILITY_DEVICE_INFO not in self.capabilities() or self._capturing:
            return {}

        details: dict[str, str] = {}
        with self._lock:
            try:
                self._transport.reset_input()
                self._transport.write(protocol.command_packet(protocol.CMD_DEVICE_INFO))
                while True:
                    line = self._transport.read_line(timeout=5.0)
                    if not line or line.startswith("INFO_END"):
                        break
                    if line.startswith("INFO:"):
                        key, _, value = line[len("INFO:"):].partition(":")
                        details[key.strip()] = value.strip()
            except (TransportError, OSError) as error:
                log.debug("Device info query failed: %s", error)
        return details

    def run_self_test(self, line_timeout: float = 20.0) -> list[SelfTestResult]:
        """Run the firmware self-test; no signals may be connected to the board."""
        if CAPABILITY_SELF_TEST not in self.capabilities():
            raise UnsupportedFeatureError("The firmware of the device has no self-test.")

        results: list[SelfTestResult] = []
        with self._lock:
            if self._capturing:
                raise DeviceConnectionError("The device is capturing.")
            self._transport.write(protocol.command_packet(protocol.CMD_SELF_TEST))
            while True:
                line = self._transport.read_line(timeout=line_timeout)
                if not line:
                    raise DeviceConnectionError("The device stopped answering during the self-test.")
                if line.startswith("SELFTEST_END"):
                    break
                result = parse_self_test_line(line)
                if result is not None:
                    results.append(result)
        return results

    # ------------------------------------------------------------- bootloader
    def enter_bootloader(self) -> bool:
        if self._capturing:
            return False
        try:
            self._transport.write(protocol.command_packet(protocol.CMD_ENTER_BOOTLOADER))
            return self._transport.read_line(timeout=10.0) == "RESTARTING_BOOTLOADER"
        except Exception:
            return False

    # ---------------------------------------------------------------- network
    def send_network_config(self, access_point: str, password: str, address: str, port: int) -> bool:
        if self._is_network:
            return False
        try:
            payload = protocol.pack_net_config(access_point, password, address, port)
            self._transport.write(protocol.command_packet(protocol.CMD_SET_WIFI, payload))
            return self._transport.read_line(timeout=5.0) == "SETTINGS_SAVED"
        except Exception:
            return False

    def get_voltage_status(self) -> Optional[str]:
        if not self._is_network:
            return "UNSUPPORTED"
        if self._capturing:
            return None
        try:
            self._transport.write(protocol.command_packet(protocol.CMD_GET_VOLTAGE))
            return self._transport.read_line(timeout=5.0)
        except Exception:
            return "DISCONNECTED"

    # ---------------------------------------------------------------- cleanup
    def dispose(self) -> None:
        if self._capturing:
            # Leave the device idle instead of capturing into the void (V6_5).
            try:
                self._transport.write(bytes([protocol.CMD_ABORT_CAPTURE]))
            except Exception:  # pragma: no cover - device may already be gone
                pass
        self._capturing = False
        self._abort.set()
        try:
            self._transport.close()
        except Exception:  # pragma: no cover - best effort
            pass
        super().dispose()
