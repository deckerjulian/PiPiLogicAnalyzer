# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Driver for an Arduino with the openSciLab firmware (``docs/protocols.md``, *Arduino firmware*).

The board describes itself (``HELLO``, ``CAPS``, ``PINS``); captures are configured with
``CAPTURE_SETUP`` and arrive as ``DATA`` events (digital run lengths, analog values, state
frames) until ``DONE``. Stream and state captures report progress while they run. The facets
beyond the captures are in :mod:`.instrument`.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Optional, Sequence

import numpy as np

from . import protocol
from .link import ArduinoError, ByteLink, Connection, LinkError, SerialLink
from ..base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_GPIO,
    CAPABILITY_STATE_MODE,
    CAPABILITY_STREAM,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    CaptureProgressArgs,
    DeviceConnectionError,
    DeviceSection,
    FirmwareOutdatedError,
    capability_value,
    stream_sample_bytes,
)
from ..models import CaptureSession, EdgeKind, TriggerType
from ...core.instrument import PinInfo, parse_pin_line

log = logging.getLogger("openscilab.driver")

#: seconds between two progress events of a stream
PROGRESS_INTERVAL_S = 0.1
#: seconds between two heartbeats (the watchdog runs out after 1 s)
HEARTBEAT_INTERVAL_S = 0.3


class _Samples:
    """Values arriving block by block, as one growing array (views stay valid)."""

    def __init__(self, dtype) -> None:
        self._data = np.zeros(4096, dtype=dtype)
        self.size = 0

    def append(self, values: np.ndarray) -> None:
        if self.size + len(values) > len(self._data):
            fresh = np.zeros(max(2 * (self.size + len(values)), 4096), dtype=self._data.dtype)
            fresh[:self.size] = self._data[:self.size]
            self._data = fresh
        self._data[self.size:self.size + len(values)] = values
        self.size += len(values)

    def view(self) -> np.ndarray:
        return self._data[:self.size]


class _Capture:
    def __init__(self, session: CaptureSession, handler, mode: int, pins: dict[int, int],
                 adc: list[int], stream: bool) -> None:
        self.session = session
        self.handler = handler
        self.mode = mode
        #: channel number -> pin index (the bit in the levels)
        self.pins = pins
        self.adc = adc
        self.stream = stream
        self.levels = _Samples(np.uint32)
        self.analog = _Samples(np.uint16)
        self.times = _Samples(np.float64)
        self.reported = 0.0
        self.stopped = False
        self.rate = session.frequency


class ArduinoDriver(AnalyzerDriverBase):
    """One Arduino board on a :class:`~.link.ByteLink`."""

    def __init__(self, link: ByteLink, address: str = "", name: str = "Arduino") -> None:
        super().__init__()
        self._address = address
        self.connection = Connection(link, name)
        try:
            self.info = self.connection.hello()
            if self.info.get("protocol") != str(protocol.PROTOCOL):
                raise FirmwareOutdatedError(
                    f"The board runs protocol {self.info.get('protocol', '?')} of the openSciLab Arduino "
                    f"firmware; this version of openSciLab works with protocol {protocol.PROTOCOL}. "
                    "Install the firmware that comes with the application.",
                    self.info.get("version", ""), address)
            text = self.connection.request(protocol.CAPS).decode("ascii", "replace")
            from ..pico.protocol import split_capabilities

            self._capabilities = frozenset(split_capabilities(text))
            self.pins = self._read_pins()
        except (LinkError, ArduinoError) as error:
            self.connection.close()
            raise DeviceConnectionError(f"{address or 'The board'}: {error}") from error
        except Exception:
            self.connection.close()
            raise
        self._channels = sorted((pin.channel, index) for index, pin in enumerate(self.pins) if pin.channel is not None)
        self._capture: Optional[_Capture] = None
        self._lock = threading.Lock()
        self._facets: Optional[list] = None
        self._keepalive = None
        self.connection.event_handlers[protocol.EVENT_DATA] = self._on_data
        self.connection.event_handlers[protocol.EVENT_DONE] = self._on_done
        self.connection.event_handlers[-1] = self._on_lost

    def _read_pins(self) -> list[PinInfo]:
        pins = []
        for index in range(256):
            line = self.connection.request(protocol.PINS, bytes([index])).decode("ascii", "replace")
            if not line:
                break
            info = parse_pin_line(line)
            if info is None:
                raise DeviceConnectionError(f"unreadable pin {index}: {line!r}")
            pins.append(info)
        return pins

    # -------------------------------------------------------------- identity
    @property
    def device_version(self) -> Optional[str]:
        return f"openSciLab Arduino {self.info.get('board', '?')} {self.info.get('version', '')}".strip()

    @property
    def address(self) -> Optional[str]:
        return self._address or None

    @property
    def driver_id(self) -> str:
        return "arduino-" + self.info.get("board", "board").lower().replace(" ", "-")

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.OTHER

    def _info_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.info.get(key, default)))
        except ValueError:
            return default

    @property
    def max_frequency(self) -> int:
        return self._info_int("rate", 100_000)

    @property
    def min_frequency(self) -> int:
        return 1

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def channel_count(self) -> int:
        return (self._channels[-1][0] + 1) if self._channels else 0

    def channel_names(self) -> list[str]:
        names = [f"CH{index}" for index in range(self.channel_count)]
        for channel, index in self._channels:
            names[channel] = self.pins[index].name
        return names

    @property
    def buffer_size(self) -> int:
        return self._info_int("buffer", 1024)

    @property
    def is_capturing(self) -> bool:
        return self._capture is not None

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def pin_index(self, name: str) -> int:
        for index, pin in enumerate(self.pins):
            if pin.name == name:
                return index
        raise KeyError(name)

    def analog_channel_names(self) -> list[str]:
        named = sorted((pin.analog_channel, pin.name) for pin in self.pins if pin.analog_channel is not None)
        names = [name for _number, name in named]
        return names[:self.analog_channel_count] if self.analog_channel_count else []

    def adc_scale(self) -> float:
        """Volts of one ADC count."""
        return self._info_int("vref_mv", 5000) / 1000.0 / (1 << self._info_int("adc_bits", 10))

    def describe(self) -> list[DeviceSection]:
        rows = [(key, value) for key, value in self.info.items()]
        rows.append(("Pins", str(len(self.pins))))
        return [("openSciLab Arduino", rows)]

    def device_details(self) -> dict[str, str]:
        return dict(self.info)

    # ---------------------------------------------------------------- limits
    @property
    def stream_bandwidth(self) -> int:
        value = capability_value(self._capabilities, CAPABILITY_STREAM + "=")
        try:
            return int(value) if value else 0
        except ValueError:
            return 0

    def acquisition_modes(self) -> tuple[str, ...]:
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM) if self.stream_bandwidth else ()

    def _bytes_per_sample(self, digital: int, analog: int = 0) -> int:
        return max((digital + 7) // 8, 1) + 2 * analog

    def max_frequency_for(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> int:
        if acquisition_mode == ACQUISITION_STREAM and self.stream_bandwidth:
            return max(min(self.stream_bandwidth // self._bytes_per_sample(len(list(channels))), self.max_frequency), 1)
        return self.max_frequency

    def memory_depth(self, digital: int, analog: int) -> Optional[int]:
        return self.buffer_size // self._bytes_per_sample(digital, analog)

    def get_limits(self, channels, acquisition_mode=None, *, to_disk=False, continuous=False) -> CaptureLimits:
        if acquisition_mode == ACQUISITION_STREAM:
            total = max(stream_sample_bytes(to_disk, continuous) // max(len(list(channels)), 1), 1)
            return CaptureLimits(0, 0, 1, total)
        depth = self.memory_depth(len(list(channels)), 0) or 1
        return CaptureLimits(min_pre_samples=0, max_pre_samples=depth // 2, min_post_samples=1,
                             max_post_samples=depth)

    def edge_trigger_channels(self) -> list[int]:
        return [channel for channel, _index in self._channels]

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        return ((0, self.channel_count),) if self.channel_count else ()

    def supports_state_mode(self) -> bool:
        return CAPABILITY_STATE_MODE in self._capabilities

    def state_clock_channels(self) -> list[int]:
        if not self.supports_state_mode():
            return []
        return [pin.channel for pin in self.pins
                if pin.channel is not None and ({"CLOCK", "CLOCK_SW"} & pin.capabilities)]

    # --------------------------------------------------------------- capture
    def start_capture(self, session: CaptureSession,
                      completed_handler: Optional[CaptureCompletedHandler] = None) -> CaptureError:
        with self._lock:
            if self._capture is not None:
                return CaptureError.BUSY
            channel_pins = dict((channel, index) for channel, index in self._channels)
            numbers = session.channel_numbers
            if not numbers or any(number not in channel_pins for number in numbers):
                return CaptureError.BAD_PARAMS
            adc = sorted({channel.channel_number for channel in session.analog_channels})
            if any(not 0 <= number < self.analog_channel_count for number in adc):
                return CaptureError.BAD_PARAMS
            stream = session.acquisition_mode == ACQUISITION_STREAM
            state = session.clock_channel is not None
            try:
                setup = self._setup(session, channel_pins, adc, stream, state)
            except ValueError as error:
                log.debug("Arduino capture rejected: %s", error)
                return CaptureError.BAD_PARAMS
            except _Unsupported:
                return CaptureError.UNSUPPORTED
            mode = protocol.MODE_STATE if state else protocol.MODE_STREAM if stream else protocol.MODE_BUFFER
            capture = _Capture(session, completed_handler, mode, {n: channel_pins[n] for n in numbers}, adc,
                               stream or state)
            try:
                rate = struct.unpack("<I", self.connection.request(protocol.CAPTURE_SETUP, setup)[:4])[0]
                capture.rate = rate or session.frequency
                self._capture = capture
                self.connection.request(protocol.CAPTURE_START)
            except ArduinoError as error:
                self._capture = None
                log.debug("Arduino capture refused: %s", error)
                return CaptureError.BUSY if error.code == protocol.ERR_BUSY else (
                    CaptureError.UNSUPPORTED if error.code == protocol.ERR_UNSUPPORTED else CaptureError.BAD_PARAMS)
            except LinkError as error:
                self._capture = None
                log.debug("Starting the Arduino capture failed: %s", error)
                return CaptureError.HARDWARE_ERROR
            if not state:
                session.frequency = int(capture.rate)
            return CaptureError.NONE

    def _setup(self, session: CaptureSession, channel_pins: dict[int, int], adc: list[int],
               stream: bool, state: bool) -> bytes:
        mask = sum(1 << channel_pins[number] for number in session.channel_numbers)
        adc_mask = sum(1 << number for number in adc)
        trigger, trigger_mask, trigger_levels = 0, 0, 0
        pre = 0
        if state:
            if not self.supports_state_mode() or session.clock_channel not in channel_pins:
                raise _Unsupported()
            if EdgeKind(session.clock_edge) == EdgeKind.ANY:
                raise ValueError("one edge of the clock only")
            samples = 0 if (stream and session.continuous) else session.post_trigger_samples
        elif stream:
            if session.trigger_type not in (TriggerType.IMMEDIATE,):
                raise _Unsupported()
            if not 1 <= session.frequency <= self.max_frequency_for(session.channel_numbers, ACQUISITION_STREAM):
                raise ValueError("rate")
            samples = 0 if session.continuous else session.post_trigger_samples
        else:
            if not 1 <= session.frequency <= self.max_frequency or session.loop_count:
                raise ValueError("rate or bursts")
            pre = session.pre_trigger_samples
            samples = pre + session.post_trigger_samples
            if samples > (self.memory_depth(len(session.channel_numbers), len(adc)) or 0):
                raise ValueError("more samples than the board holds")
            kind = session.trigger_type
            if kind == TriggerType.IMMEDIATE:
                pre = 0
            elif kind in (TriggerType.EDGE,):
                if session.trigger_channel not in channel_pins:
                    raise ValueError("trigger channel")
                trigger = 2 if session.trigger_inverted else 1
                trigger_mask = 1 << channel_pins[session.trigger_channel]
            elif kind in (TriggerType.COMPLEX, TriggerType.FAST):
                trigger = 3
                for offset in range(max(session.trigger_bit_count, 1)):
                    channel = session.trigger_channel + offset
                    if session.trigger_mask is not None and not (session.trigger_mask >> offset) & 1:
                        continue  # (don't care: not in the mask)
                    if channel not in channel_pins:
                        raise ValueError("pattern channel")
                    trigger_mask |= 1 << channel_pins[channel]
                    if (session.trigger_pattern >> offset) & 1:
                        trigger_levels |= 1 << channel_pins[channel]
            else:
                raise _Unsupported()
        mode = protocol.MODE_STATE if state else protocol.MODE_STREAM if stream else protocol.MODE_BUFFER
        clock = channel_pins.get(session.clock_channel, 0xFF) if state else 0xFF
        edge = 1 if state and EdgeKind(session.clock_edge) == EdgeKind.FALLING else 0
        return struct.pack("<BIIHIIBIIBB", mode, max(int(session.frequency), 0), mask, adc_mask, samples, pre,
                           trigger, trigger_mask, trigger_levels, clock, edge)

    def _on_data(self, frame: protocol.Frame) -> None:
        capture = self._capture
        if capture is None or len(frame.data) < 5:
            return
        kind = frame.data[0]
        body = frame.data[5:]  # after the kind and the index of the first sample
        if kind == protocol.DATA_DIGITAL:
            runs = np.frombuffer(body[:len(body) - len(body) % 6], dtype=np.dtype([("count", "<u2"), ("levels", "<u4")]))
            capture.levels.append(np.repeat(runs["levels"], runs["count"].astype(np.int64)))
        elif kind == protocol.DATA_ANALOG:
            capture.analog.append(np.frombuffer(body[:len(body) - len(body) % 2], dtype="<u2"))
        elif kind == protocol.DATA_STATE:
            size = 8 + 2 * len(capture.adc)
            for offset in range(0, len(body) - size + 1, size):
                stamp, levels = struct.unpack_from("<II", body, offset)
                capture.times.append(np.asarray([stamp], dtype=np.float64))
                capture.levels.append(np.asarray([levels], dtype=np.uint32))
                if capture.adc:
                    capture.analog.append(np.frombuffer(body[offset + 8:offset + size], dtype="<u2"))
        if capture.stream and time.monotonic() - capture.reported >= PROGRESS_INTERVAL_S:
            capture.reported = time.monotonic()
            self._raise_capture_progress(self._progress(capture))

    def _progress(self, capture: _Capture) -> CaptureProgressArgs:
        levels = capture.levels.view()
        samples = {number: ((levels >> np.uint32(pin)) & 1).astype(np.uint8) for number, pin in capture.pins.items()}
        analog = self._analog(capture, len(levels))
        times = capture.times.view() - capture.times.view()[0] if capture.times.size else None
        return CaptureProgressArgs(capture.session, samples, 0, analog=analog, state_times=times)

    def _analog(self, capture: _Capture, count: Optional[int] = None) -> dict[int, np.ndarray]:
        if not capture.adc:
            return {}
        raw = capture.analog.view()
        raw = raw[:len(raw) - len(raw) % len(capture.adc)]
        return {number: raw[index::len(capture.adc)].astype(np.int16)
                for index, number in enumerate(capture.adc)}

    def _on_done(self, frame: protocol.Frame) -> None:
        capture = self._capture
        if capture is None:
            return
        status = frame.data[0] if frame.data else protocol.DONE_COMPLETE
        lost = struct.unpack_from("<I", frame.data, 5)[0] if len(frame.data) >= 9 else 0
        self._capture = None
        if capture.stopped and not capture.stream:
            return  # a stopped buffer capture reports nothing (as the other drivers)
        self._finish(capture, status, lost)

    def _finish(self, capture: _Capture, status: int, lost: int = 0, error: Optional[str] = None) -> None:
        session = capture.session
        levels = capture.levels.view().copy()
        count = len(levels)
        for channel in session.capture_channels:
            pin = capture.pins[channel.channel_number]
            channel.samples = ((levels >> np.uint32(pin)) & 1).astype(np.uint8)
        analog = self._analog(capture)
        for channel in session.analog_channels:
            channel.raw = analog.get(channel.channel_number, np.zeros(0, np.int16)).copy()
            channel.length = None
            channel.scale, channel.offset, channel.unit = self.adc_scale(), 0.0, "V"
            channel.channel_name = channel.channel_name or (self.analog_channel_names() or [""])[
                min(channel.channel_number, max(self.analog_channel_count - 1, 0))]
        if capture.mode == protocol.MODE_STATE:
            session.state_times = (capture.times.view() - capture.times.view()[0]).copy() if count else None
        if capture.stream:
            session.pre_trigger_samples = 0
            session.post_trigger_samples = count
            session.loop_count = 0
        if status == protocol.DONE_OVERFLOW and error is None:
            error = ("The board could not send the samples as fast as it took them, so the capture "
                     "stopped early. Lower the rate or capture fewer channels.")
        elif lost and error is None:
            error = f"{lost:,} clock edges came too fast and were lost."
        self._raise_capture_completed(CaptureCompletedArgs(success=count > 0, session=session, error=error),
                                      capture.handler)

    def _on_lost(self, _frame: protocol.Frame) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            self._finish(capture, protocol.DONE_STOPPED, error=f"The board was lost: {self.connection.error}")

    def stop_capture(self) -> bool:
        capture = self._capture
        if capture is None:
            return False
        capture.stopped = True
        try:
            self.connection.request(protocol.CAPTURE_ABORT)
        except (LinkError, ArduinoError) as error:
            log.debug("Stopping the Arduino capture failed: %s", error)
            if self._capture is capture:
                self._capture = None
                if capture.stream:
                    self._finish(capture, protocol.DONE_STOPPED)
        return True

    # ---------------------------------------------------------------- facets
    def heartbeat(self) -> None:
        self.connection.notify(protocol.HEARTBEAT)

    def instrument_facets(self) -> list:
        if self._facets is None:
            from ...core.instrument import KeepAlive
            from .instrument import facets_for

            self._facets = facets_for(self)
            if CAPABILITY_GPIO in self._capabilities and self._keepalive is None:
                self._keepalive = KeepAlive(self._beat, HEARTBEAT_INTERVAL_S)
                self._keepalive.start()
        return list(self._facets)

    def _beat(self) -> None:
        from ...core.instrument import GpioFacet

        for facet in self._facets or ():
            if isinstance(facet, GpioFacet) and not facet.keepalive:
                return
        if self.connection.alive:
            self.heartbeat()

    def dispose(self) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None
        if self._capture is not None:
            try:
                self.connection.request(protocol.CAPTURE_ABORT, timeout=0.5)
            except (LinkError, ArduinoError):
                pass
            self._capture = None
        self.connection.close()
        super().dispose()


class _Unsupported(Exception):
    pass


def open_serial(port: str, baud: Optional[int] = None) -> ArduinoDriver:
    """The Arduino on ``port`` (baud rate from its USB identifiers when not given)."""
    from .. import ports

    if baud is None:
        details = next((item for item in ports.detect_arduinos() if item.port_name == port), None)
        baud = details.baud if details is not None else ports.ARDUINO_AVR_BAUD
    try:
        link = SerialLink(port, baud)
    except LinkError as error:
        raise DeviceConnectionError(str(error)) from error
    return ArduinoDriver(link, address=f"arduino:{port}", name=f"Arduino on {port}")
