# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Driver for a single PiPiLogicAnalyzer device (serial or network).

Port of ``SharedDriver/LogicAnalyzerDriver.cs`` of the original LogicAnalyzer.

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
* only boards with the firmware of this application are opened (``PROTOCOL:<n>`` of the
  identification, see ``protocol.FIRMWARE_PROTOCOL``); others have to be updated first;
* trigger sequences (``TriggerType.SEQUENCE``) and the state mode
  (``CaptureSession.clock_channel``) of this project's firmware: ``CMD_TRIGGER_SEQUENCE``
  configures them, a capture request with trigger type 7 starts the capture. Both are only
  offered when the firmware reports ``TRIGGER_SEQUENCE`` / ``STATE_MODE``.

Driver activity is logged through :mod:`logging` (``pipilogicanalyzer.driver``),
the counterpart of the ``DEBUG_MODE`` log added in V6_5.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional, Sequence

import numpy as np

from . import info, protocol
from ..base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_CONTINUOUS_STREAM,
    CAPABILITY_STREAM,
    CAPABILITY_STREAM_IMMEDIATE_ONLY,
    CaptureLimits,
    CaptureProgressArgs,
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
    CAPABILITY_STATE_MODE,
    CAPABILITY_TRIGGER_SEQUENCE,
    CaptureMode,
    DeviceConnectionError,
    DeviceSection,
    FirmwareOutdatedError,
    SelfTestResult,
    UnsupportedFeatureError,
    pattern_fits,
    pattern_max_bits,
    DISK_FULL_ERROR,
    DiskWatch,
    stream_sample_bytes,
)
from .protocol import (
    COMPLEX_TRIGGER_DELAY,
    EDGE_OUT_TRIGGER_DELAY,
    FAST_TRIGGER_DELAY,
    SEQUENCE_EDGES,
    SEQUENCE_KINDS,
    SEQUENCE_MAX_SAMPLES,
    SEQUENCE_NO_LIMIT,
    SequenceRequest,
    SequenceStage,
    ns_to_samples,
    parse_pattern_groups,
    parse_self_test_line,
    parse_state_max_clock,
    parse_trigger_sequence,
    parse_version,
    trigger_delay_samples,
)
from ...core.sample_store import DiskAllocator, MemoryAllocator, RingStore, SampleStore
from ...core.simulation import SimulationPattern
from ..models import (
    AnalyzerChannel,
    BurstInfo,
    CaptureSession,
    ConditionKind,
    EdgeKind,
    TriggerCondition,
    TriggerSequence,
    TriggerStage,
    TriggerType,
)
from .transport import NetworkTransport, SerialTransport, Transport, TransportError

_ADDRESS_PORT_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+):(\d+)$")
_CHANNELS_RE = re.compile(r"^CHANNELS:(\d+)$")
_PROTOCOL_RE = re.compile(r"^PROTOCOL:(\d+)$")
#: Seconds to wait for the protocol line, which older firmware does not send
PROTOCOL_TIMEOUT_S = 1.5
_BUFFER_RE = re.compile(r"^BUFFER:(\d+)$")
_FREQ_RE = re.compile(r"^FREQ:(\d+)$")
_BLAST_RE = re.compile(r"^BLASTFREQ:(\d+)$")
_STREAM_STARTED_RE = re.compile(r"^STREAM_STARTED:([\d,]+)$")

#: Trigger type of a stream capture request: raw input words sent while capturing, in chunks of
#: a 32 bit byte count and the data, until any byte from the host stops it
STREAM_TRIGGER_TYPE = 6
STREAM_END_STOPPED = 0
#: USB was too slow: the chunk before the marker may hold overwritten samples
STREAM_END_OVERFLOW = 0xFFFFFFFF
#: Bytes per second of firmware that announces ``STREAM`` without a value
DEFAULT_STREAM_BANDWIDTH = 800_000
STREAM_STALL_TIMEOUT = 3.0
STREAM_STOP_TIMEOUT = 3.0
#: Seconds between two progress events of a stream (live display)
STREAM_PROGRESS_INTERVAL = 0.1

#: The firmware ends a trigger sequence capture without samples when its evaluation fell behind
SEQUENCE_OVERFLOW_ERROR = (
    "The trigger sequence could not be evaluated as fast as the samples arrived, so the capture "
    "was stopped without samples. Lower the sample rate or use channels that change less often."
)
#: Trigger types of a state mode capture (all evaluated as a trigger sequence by the firmware)
STATE_MODE_TRIGGERS = (
    TriggerType.IMMEDIATE,
    TriggerType.EDGE,
    TriggerType.COMPLEX,
    TriggerType.FAST,
    TriggerType.SEQUENCE,
)
#: Conditions of a state mode sequence: the samples have no time base
STATE_MODE_CONDITIONS = frozenset({ConditionKind.PATTERN.value, ConditionKind.EDGE.value})

#: Systick period used when the device reports no blast frequency.  The systick
#: runs at the CPU clock, which is the blast frequency (200MHz -> 5ns on RP2040).
NS_PER_TICK = 5.0

log = logging.getLogger("pipilogicanalyzer.driver")


def unpack_channel_samples(raw_samples: np.ndarray, channel_index: int) -> np.ndarray:
    """Extract the samples of one channel from the packed capture words."""
    return ((raw_samples >> np.uint32(channel_index)) & np.uint32(1)).astype(np.uint8)


#: Bit of every channel in the samples of the LogicAnalyzer Interceptor. Its channels do not start at
#: the first input GPIO (PIN_MAP of BOARD_INTERCEPTOR: channel 0 = GPIO 6, INPUT_PIN_BASE = 2), so
#: the capture mode has to be chosen by these bits, not by the channel number.
INTERCEPTOR_SAMPLE_BITS = tuple(range(4, 28)) + (0, 1, 2, 3)


#: Edge trigger inputs of the single board firmware
EDGE_TRIGGER_INPUTS = 24

SELF_TEST_DESCRIPTION = (
    "The test checks the capture buffer, the trigger link and every channel input "
    "using only the internal pull resistors, and records the pull pattern through the "
    "normal and the blast capture path. Boards with input buffers or level shifters "
    "report their channels as driven; that is expected there."
)


class PiPiLogicAnalyzerDriver(AnalyzerDriverBase):
    """Talks to one device over USB CDC or over TCP."""

    def __init__(
        self, connection_string: str, connect_timeout: float = 10.0, require_current_firmware: bool = True
    ) -> None:
        """Opens the board; raises ``FirmwareOutdatedError`` unless it runs the current firmware.

        ``require_current_firmware=False`` opens a board with any firmware, only to read its
        identification or to restart it into the bootloader for an update.
        """
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
        self._request_layout = protocol.REQUEST_LAYOUT
        #: ``PROTOCOL:<n>`` of the identification (``None``: older firmware without it)
        self.protocol_version: Optional[int] = None
        self._require_current_firmware = require_current_firmware
        self._capabilities: Optional[frozenset[str]] = None
        self._streaming = False
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
        if not parse_version(self._version).major:
            raise DeviceConnectionError(f"Invalid device identification {self._version!r}")

        self._max_frequency = self._read_int_response(_FREQ_RE, "frequency")
        self._blast_frequency = self._read_int_response(_BLAST_RE, "blast frequency")
        self._buffer_size = self._read_int_response(_BUFFER_RE, "buffer size")
        self._channel_count = self._read_int_response(_CHANNELS_RE, "channel count")
        # Older firmware ends the identification with the channels
        try:
            match = _PROTOCOL_RE.match(self._transport.read_line(timeout=PROTOCOL_TIMEOUT_S) or "")
        except TransportError:
            match = None
        self.protocol_version = int(match.group(1)) if match else None
        if self._require_current_firmware and self.protocol_version != protocol.FIRMWARE_PROTOCOL:
            raise FirmwareOutdatedError(
                f"The board runs {self._version}, which this version of PiPiLogicAnalyzer does not "
                "work with. Install the firmware that comes with the application "
                "(Device > Install or update firmware).",
                self._version or "",
                self.connection_string,
            )
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

    def sample_bits(self, channels: Sequence[int]) -> list[int]:
        if "_INTERCEPTOR_" in (self._version or ""):
            return [
                INTERCEPTOR_SAMPLE_BITS[channel] if 0 <= channel < len(INTERCEPTOR_SAMPLE_BITS) else channel
                for channel in channels
            ]
        return super().sample_bits(channels)

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

    @property
    def supports_bootloader(self) -> bool:
        return True

    @property
    def supports_network_config(self) -> bool:
        return not self._is_network and "WIFI" in (self._version or "")

    def edge_trigger_channels(self) -> list[int]:
        """The firmware of a single board has 24 edge trigger inputs."""
        return list(range(min(self.channel_count, EDGE_TRIGGER_INPUTS)))

    @property
    def has_self_test(self) -> bool:
        return True

    @property
    def self_test_description(self) -> str:
        return SELF_TEST_DESCRIPTION

    def describe(self) -> list[DeviceSection]:
        return info.describe_board(self)


    # ---------------------------------------------------------------- capture
    def start_capture(
        self,
        session: CaptureSession,
        completed_handler: Optional[CaptureCompletedHandler] = None,
        internal_test: bool = False,
    ) -> CaptureError:
        """``internal_test``: a stream of the firmware's test counter instead of the inputs."""
        with self._lock:
            if self._capturing:
                return CaptureError.BUSY
            if not session.capture_channels:
                return CaptureError.BAD_PARAMS
            if session.acquisition_mode == ACQUISITION_STREAM:
                if session.clock_channel is not None:
                    return CaptureError.UNSUPPORTED  # the stream samples at its rate only
                return self._start_stream(session, completed_handler, internal_test)
            if session.trigger_type == TriggerType.SEQUENCE or session.clock_channel is not None:
                return self._start_sequence(session, completed_handler)

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
            if length == 0:
                # Only a trigger sequence capture ends without samples: its evaluation fell behind
                self._read_timestamps()
                raise RuntimeError(SEQUENCE_OVERFLOW_ERROR)

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

    # ------------------------------------------------- trigger sequence / state
    def trigger_sequence_limits(self) -> Optional[tuple[int, frozenset[str], int]]:
        """Stages, condition kinds (``ConditionKind`` values) and highest sample rate of the
        firmware's trigger sequences; ``None`` if the firmware has none."""
        limits = parse_trigger_sequence(self.capabilities())
        if limits is None:
            return None
        stages, kinds, rate = limits
        return stages, kinds, min(rate, self.max_frequency) if rate else self.max_frequency

    def supports_state_mode(self) -> bool:
        """Samples on the edges of a clock channel (``CaptureSession.clock_channel``)."""
        return CAPABILITY_STATE_MODE in self.capabilities()

    @property
    def state_max_clock(self) -> int:
        """Highest clock of the state mode in Hz; 0 without state mode or when not reported."""
        if not self.supports_state_mode():
            return 0
        return parse_state_max_clock(self.capabilities()) or 0

    def state_clock_channels(self) -> list[int]:
        """Channels usable as the clock of the state mode: every channel input (not the external
        trigger input)."""
        return list(range(self.channel_count)) if self.supports_state_mode() else []

    def _sequence_stages(
        self, session: CaptureSession, state_mode: bool
    ) -> tuple[CaptureError, list[SequenceStage], list[int]]:
        """Stages of the capture in samples and the channels they look at.

        In state mode the edge and pattern triggers become a sequence of one stage and the
        immediate trigger one without stages.
        """
        trigger = session.trigger_type
        if state_mode:
            if trigger not in STATE_MODE_TRIGGERS:
                return CaptureError.BAD_PARAMS, [], []
            if trigger == TriggerType.IMMEDIATE:
                return CaptureError.NONE, [], []
            if trigger == TriggerType.EDGE:
                sequence = TriggerSequence([TriggerStage(TriggerCondition(
                    kind=ConditionKind.EDGE,
                    channel=session.trigger_channel,
                    edge=EdgeKind.FALLING if session.trigger_inverted else EdgeKind.RISING,
                ))])
            elif trigger in (TriggerType.COMPLEX, TriggerType.FAST):
                bits = session.trigger_bit_count
                if not 1 <= bits <= 32:
                    return CaptureError.BAD_PARAMS, [], []
                mask = ((1 << bits) - 1) << session.trigger_channel
                value = (session.trigger_pattern & ((1 << bits) - 1)) << session.trigger_channel
                sequence = TriggerSequence([TriggerStage(TriggerCondition(
                    kind=ConditionKind.PATTERN, mask=mask, value=value
                ))])
            else:
                sequence = session.trigger_sequence or TriggerSequence()
        else:
            sequence = session.trigger_sequence or TriggerSequence()

        limits = self.trigger_sequence_limits()
        if limits is None:
            # State mode without sequences in the firmware: immediate captures only
            return (CaptureError.UNSUPPORTED if sequence.stages else CaptureError.NONE), [], []
        max_stages, kinds, _rate = limits
        if state_mode:
            kinds = kinds & STATE_MODE_CONDITIONS
        if not sequence.stages and not state_mode:
            return CaptureError.BAD_PARAMS, [], []
        if len(sequence.stages) > max_stages:
            return CaptureError.BAD_PARAMS, [], []

        frequency = session.frequency
        stages: list[SequenceStage] = []
        used: list[int] = []

        def samples(nanoseconds: Optional[int], round_up: bool) -> Optional[int]:
            if nanoseconds is None:
                return None
            if nanoseconds < 0:
                raise ValueError("negative time")
            value = ns_to_samples(nanoseconds, frequency, round_up)
            if value > SEQUENCE_MAX_SAMPLES:
                raise ValueError("time too long")
            return value

        for index, stage in enumerate(sequence.stages):
            condition = stage.condition
            kind = ConditionKind(condition.kind).value
            if kind not in kinds:
                if state_mode and kind in SEQUENCE_KINDS:
                    return CaptureError.BAD_PARAMS, [], []  # pulse/gap need a time base
                return CaptureError.UNSUPPORTED, [], []
            if not 1 <= stage.count <= SEQUENCE_MAX_SAMPLES:
                return CaptureError.BAD_PARAMS, [], []
            if index and stage.within_ns is not None and state_mode:
                return CaptureError.BAD_PARAMS, [], []
            entry = SequenceStage(kind=SEQUENCE_KINDS[kind], count=stage.count)
            try:
                if index and stage.within_ns is not None:
                    entry.within_samples = samples(stage.within_ns, True)
                if kind == ConditionKind.PATTERN.value:
                    if condition.mask < 0 or condition.mask >> self.channel_count:
                        return CaptureError.BAD_PARAMS, [], []
                    entry.mask = condition.mask
                    entry.value = condition.value & condition.mask
                    used.extend(bit for bit in range(self.channel_count) if condition.mask >> bit & 1)
                else:
                    if not 0 <= condition.channel < self.channel_count:
                        return CaptureError.BAD_PARAMS, [], []
                    entry.channel = condition.channel
                    entry.edge = SEQUENCE_EDGES[EdgeKind(condition.edge).value]
                    used.append(condition.channel)
                if kind == ConditionKind.PULSE.value:
                    # +-1 sample: the limits err on the side of accepting a pulse
                    minimum = samples(condition.min_ns, False)
                    maximum = samples(condition.max_ns, True)
                    entry.min_samples = minimum or 0
                    entry.max_samples = SEQUENCE_NO_LIMIT if maximum is None else maximum
                    if maximum is not None and maximum < entry.min_samples:
                        return CaptureError.BAD_PARAMS, [], []
                elif kind == ConditionKind.GAP.value:
                    if not condition.min_ns:
                        return CaptureError.BAD_PARAMS, [], []
                    entry.min_samples = max(samples(condition.min_ns, True) or 0, 1)
            except ValueError:
                return CaptureError.BAD_PARAMS, [], []
            stages.append(entry)
        return CaptureError.NONE, stages, used

    def _start_sequence(
        self, session: CaptureSession, completed_handler: Optional[CaptureCompletedHandler]
    ) -> CaptureError:
        state_mode = session.clock_channel is not None
        if state_mode and not self.supports_state_mode():
            return CaptureError.UNSUPPORTED
        if session.trigger_type == TriggerType.SEQUENCE and self.trigger_sequence_limits() is None:
            return CaptureError.UNSUPPORTED

        error, stages, trigger_channels = self._sequence_stages(session, state_mode)
        if error != CaptureError.NONE:
            log.debug("Sequence capture rejected: %s", error.value)
            return error

        channels = session.channel_numbers
        if min(channels) < 0 or max(channels) >= self.channel_count:
            return CaptureError.BAD_PARAMS
        if state_mode and (
            not 0 <= session.clock_channel < self.channel_count
            or EdgeKind(session.clock_edge) == EdgeKind.ANY  # one edge of the clock only
        ):
            return CaptureError.BAD_PARAMS

        # The firmware evaluates the stages on the samples: their channels must be part of them
        mode = self.get_capture_mode(list(channels) + trigger_channels)
        limits = self.get_limits(list(channels) + trigger_channels)
        if not (
            session.loop_count == 0
            and limits.min_pre_samples <= session.pre_trigger_samples <= limits.max_pre_samples
            and limits.min_post_samples <= session.post_trigger_samples <= limits.max_post_samples
            and session.pre_trigger_samples + session.post_trigger_samples <= limits.max_total_samples
        ):
            log.debug("Sequence capture rejected: invalid sample counts")
            return CaptureError.BAD_PARAMS
        if not state_mode:
            limit = self.trigger_sequence_limits()
            max_rate = limit[2] if limit else self.max_frequency
            if not self.min_frequency <= session.frequency <= max_rate:
                log.debug("Sequence capture rejected: %d Hz, at most %d Hz", session.frequency, max_rate)
                return CaptureError.BAD_PARAMS

        sequence = SequenceRequest(
            stages=stages,
            state_mode=state_mode,
            clock_channel=session.clock_channel if state_mode else 0,
            clock_falling=state_mode and EdgeKind(session.clock_edge) == EdgeKind.FALLING,
        )
        request = protocol.CaptureRequest(
            trigger_type=protocol.SEQUENCE_TRIGGER_TYPE,
            channels=channels,
            channel_count=len(channels),
            # Ignored by the firmware in state mode
            frequency=max(session.frequency, 0),
            pre_samples=session.pre_trigger_samples,
            post_samples=session.post_trigger_samples,
            capture_mode=int(mode),
        )

        try:
            self._transport.reset_input()
            self._transport.write(protocol.command_packet(protocol.CMD_TRIGGER_SEQUENCE, sequence.pack()))
            answer = self._transport.read_line(timeout=10.0)
            if answer != "SEQUENCE_OK":
                log.debug("Error configuring the trigger sequence, device response: %r", answer)
                return CaptureError.HARDWARE_ERROR
            self._transport.write(
                protocol.command_packet(protocol.CMD_START_CAPTURE, request.pack(self._request_layout))
            )
            result = self._transport.read_line(timeout=10.0)
        except (TransportError, OSError) as error:
            log.debug("Error starting the sequence capture: %s", error)
            return CaptureError.HARDWARE_ERROR

        if result != "CAPTURE_STARTED":
            log.debug("Error starting the sequence capture, device response: %r", result)
            return CaptureError.HARDWARE_ERROR
        log.debug(
            "%s capture started (%d stages, mode %s)",
            "State" if state_mode else "Sequence", len(stages), mode.name,
        )

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

    # ----------------------------------------------------------------- stream
    @property
    def stream_bandwidth(self) -> int:
        """Bytes per second the firmware streams over USB; 0 without stream captures."""
        if self._is_network:
            return 0
        for item in self.capabilities():
            if item == CAPABILITY_STREAM:
                return DEFAULT_STREAM_BANDWIDTH
            if item.startswith(CAPABILITY_STREAM + "="):
                try:
                    return max(int(item[len(CAPABILITY_STREAM) + 1:]), 0)
                except ValueError:
                    return DEFAULT_STREAM_BANDWIDTH
        return 0

    def acquisition_modes(self) -> tuple[str, ...]:
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM) if self.stream_bandwidth else ()

    def max_frequency_for(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> int:
        if acquisition_mode != ACQUISITION_STREAM:
            return self.max_frequency
        mode = self.get_capture_mode(channels or [0])
        return min(self.stream_bandwidth // mode.bytes_per_sample, self.max_frequency)

    def get_limits(
        self,
        channels: Sequence[int],
        acquisition_mode: Optional[str] = None,
        *,
        to_disk: bool = False,
        continuous: bool = False,
    ) -> CaptureLimits:
        if acquisition_mode != ACQUISITION_STREAM:
            return super().get_limits(channels, acquisition_mode)
        total = max(stream_sample_bytes(to_disk, continuous) // max(len(list(channels)), 1), 1)
        return CaptureLimits(min_pre_samples=0, max_pre_samples=0, min_post_samples=1, max_post_samples=total)

    def _start_stream(
        self,
        session: CaptureSession,
        completed_handler: Optional[CaptureCompletedHandler],
        internal_test: bool = False,
    ) -> CaptureError:
        channels = session.channel_numbers
        limits = self.get_limits(
            channels, ACQUISITION_STREAM, to_disk=session.to_disk, continuous=session.continuous
        )
        if not (
            self.stream_bandwidth
            and session.trigger_type == TriggerType.IMMEDIATE
            and min(channels) >= 0
            and max(channels) < self.channel_count
            and session.loop_count == 0
            and 1 <= session.post_trigger_samples <= limits.max_post_samples
            and 1 <= session.frequency <= self.max_frequency_for(channels, ACQUISITION_STREAM)
        ):
            log.debug("Stream rejected: invalid settings")
            return CaptureError.BAD_PARAMS

        mode = self.get_capture_mode(channels)
        request = protocol.CaptureRequest(
            trigger_type=STREAM_TRIGGER_TYPE,
            trigger_value=1 if internal_test else 0,
            channels=channels,
            channel_count=len(channels),
            frequency=session.frequency,
            capture_mode=int(mode),
        )
        try:
            self._transport.reset_input()
            self._transport.write(
                protocol.command_packet(protocol.CMD_START_CAPTURE, request.pack(self._request_layout))
            )
            result = self._transport.read_line(timeout=10.0)
        except (TransportError, OSError) as error:
            log.debug("Error starting the stream: %s", error)
            return CaptureError.HARDWARE_ERROR

        match = _STREAM_STARTED_RE.match(result or "")
        bits = [int(bit) for bit in match.group(1).split(",")] if match else []
        if len(bits) != len(channels):
            log.debug("Error starting the stream, device response: %r", result)
            return CaptureError.HARDWARE_ERROR
        log.debug("Stream started (%d Hz, mode %s, bits %s)", session.frequency, mode.name, bits)

        self._capturing = self._streaming = True
        self._abort.clear()
        self._capture_thread = threading.Thread(
            target=self._read_stream,
            args=(session, mode, bits, completed_handler),
            name="pipilogicanalyzer-stream",
            daemon=True,
        )
        self._capture_thread.start()
        return CaptureError.NONE

    def _read_stream(
        self,
        session: CaptureSession,
        mode: CaptureMode,
        bits: list[int],
        completed_handler: Optional[CaptureCompletedHandler],
    ) -> None:
        numbers = session.channel_numbers
        dtype = {1: "<u1", 2: "<u2", 4: "<u4"}[mode.bytes_per_sample]
        wanted = session.post_trigger_samples
        allocator = DiskAllocator() if session.to_disk else MemoryAllocator()
        if session.continuous:
            store: SampleStore = RingStore(numbers, wanted, allocator)
        else:
            store = SampleStore(numbers, wanted, allocator)

        def append(data: bytes) -> None:
            raw = np.frombuffer(data, dtype=dtype)
            store.append({number: ((raw >> bit) & 1).astype(np.uint8) for number, bit in zip(numbers, bits)})

        # A chunk is only used once the next header shows the stream did not overflow meanwhile.
        held: Optional[bytes] = None
        stop_sent = False
        overflow = disk_full = False
        reported = time.monotonic()
        watch = DiskWatch() if session.to_disk else None
        try:
            while True:
                size = int.from_bytes(self._transport.read_exactly(4, timeout=STREAM_STALL_TIMEOUT), "little")
                if size in (STREAM_END_STOPPED, STREAM_END_OVERFLOW):
                    overflow = size == STREAM_END_OVERFLOW
                    break
                data = self._transport.read_exactly(size, timeout=STREAM_STALL_TIMEOUT)
                if held is not None:
                    append(held)
                held = data
                if not session.continuous and not stop_sent and store.total + len(held) // mode.bytes_per_sample >= wanted:
                    self._transport.write(bytes([protocol.CMD_ABORT_CAPTURE]))
                    stop_sent = True
                if watch is not None and watch.low.is_set() and not stop_sent:
                    self._transport.write(bytes([protocol.CMD_ABORT_CAPTURE]))
                    stop_sent = disk_full = True
                if store.total and time.monotonic() - reported >= STREAM_PROGRESS_INTERVAL:
                    reported = time.monotonic()
                    views, first = store.window()
                    self._raise_capture_progress(CaptureProgressArgs(session, views, first))
            if held is not None and not overflow:
                append(held)
        except Exception as error:  # noqa: BLE001 - reported to the UI
            self._capturing = self._streaming = False
            if watch is not None:
                watch.stop()
            if self._abort.is_set():
                return
            log.debug("Error reading the stream: %s", error)
            self._raise_capture_completed(
                CaptureCompletedArgs(success=False, session=session, error=str(error)), completed_handler
            )
            return

        if watch is not None:
            watch.stop()
        samples, first = store.result()
        count = min((len(values) for values in samples.values()), default=0)
        for channel in session.capture_channels:
            channel.samples = samples[channel.channel_number][:count]
        session.pre_trigger_samples = 0
        session.post_trigger_samples = count
        session.loop_count = 0
        session.bursts = None
        self._capturing = self._streaming = False
        log.debug("Stream complete: %d samples%s", count, ", overflow" if overflow else "")
        error = DISK_FULL_ERROR if disk_full else None
        if overflow:
            error = (
                "The USB connection could not keep up with the stream, so it stopped early. "
                "Lower the rate or capture fewer channels."
            )
        self._raise_capture_completed(
            CaptureCompletedArgs(success=count > 0, session=session, error=error, first_sample=first),
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

        if self._streaming:
            # The firmware ends the stream with its end marker; the samples so far are kept.
            try:
                self._transport.write(bytes([protocol.CMD_ABORT_CAPTURE]))
            except Exception:  # pragma: no cover - device may already be gone
                pass
            thread = self._capture_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(STREAM_STOP_TIMEOUT)
                if not thread.is_alive():
                    return True
            log.debug("The stream did not end, reconnecting")

        self._capturing = self._streaming = False
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
        """Functions of the board, queried once and cached (they depend on the board and on
        the connection, e.g. streams only over USB)."""
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
            items = {item.strip() for item in line[len("CAPS:"):].split(",") if item.strip()}
            if not self._is_network and any(
                item == CAPABILITY_STREAM or item.startswith(CAPABILITY_STREAM + "=") for item in items
            ):
                items |= {CAPABILITY_CONTINUOUS_STREAM, CAPABILITY_STREAM_IMMEDIATE_ONLY}
            # "TRIGGER_SEQUENCE=<stages>": also the plain name, so `in capabilities()` finds it
            if any(item.startswith(CAPABILITY_TRIGGER_SEQUENCE + "=") for item in items):
                items.add(CAPABILITY_TRIGGER_SEQUENCE)
            self._capabilities = frozenset(items)
        else:
            self._capabilities = frozenset()
        log.debug("Device capabilities: %s", sorted(self._capabilities))
        return self._capabilities

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        """Channel groups reported by the firmware; none on boards without pattern triggers."""
        groups = parse_pattern_groups(self.capabilities())
        if groups is None:
            return ()
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
        if self.stream_bandwidth:
            results.append(self._stream_self_test())
        return results

    def _stream_self_test(self, seconds: float = 0.5) -> SelfTestResult:
        """A stream of the firmware's test counter at the highest rate of 8 channels."""
        rate = self.max_frequency_for(range(8), ACQUISITION_STREAM)
        session = CaptureSession(frequency=rate, pre_trigger_samples=0, post_trigger_samples=int(rate * seconds))
        session.capture_channels = [AnalyzerChannel(channel_number=number) for number in range(8)]
        session.acquisition_mode = ACQUISITION_STREAM
        session.trigger_type = TriggerType.IMMEDIATE
        done = threading.Event()
        outcome: list[CaptureCompletedArgs] = []
        error = self.start_capture(session, lambda args: (outcome.append(args), done.set()), internal_test=True)
        if error != CaptureError.NONE:
            return SelfTestResult("STREAM", "FAIL", f"The stream could not be started ({error.value})")
        if not done.wait(seconds + 10):
            self.stop_capture()
            return SelfTestResult("STREAM", "FAIL", "The stream did not end")
        result = outcome[0]
        if not result.success or result.error:
            return SelfTestResult("STREAM", "FAIL", result.error or "No samples arrived")
        # The counter runs down by one per sample
        words = sum(channel.samples.astype(np.int64) << channel.channel_number for channel in session.capture_channels)
        wrong = int(np.count_nonzero((words[:-1] - words[1:]) & 0xFF != 1))
        if wrong:
            return SelfTestResult("STREAM", "FAIL", f"{wrong:,} of {len(words):,} samples wrong")
        return SelfTestResult(
            "STREAM", "OK", f"{len(words):,} samples at {rate / 1e3:g} kHz over USB, test pattern exact"
        )

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
