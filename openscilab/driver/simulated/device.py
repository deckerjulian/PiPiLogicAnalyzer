# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The device model of a simulated instrument: a logic analyzer with the limits of its profile.

:class:`SimulatedDriver` is an :class:`~openscilab.driver.base.AnalyzerDriverBase` like the
drivers of real devices, so the analyzer, the capture dialog and the flow nodes use it unchanged.
It samples the nets of its :class:`~.circuit.Circuit`:

* **buffer**: edge, pattern or no trigger, pre-trigger samples, up to the memory depth (which
  depends on the analog channels in use), analog channels with the resolution and noise of the
  profile's ADC;
* **stream**: samples arrive in blocks while the capture runs (progress events), limited by the
  stream bandwidth of the profile; *until stopped* keeps the newest samples;
* **state**: samples on the edges of a clock input, with the time of every state, in a buffer
  or (``STREAM_STATE``) as a stream;
* **progressive** (profiles with ``progressive``): a large buffer capture is complete at once as
  an overview (computed from the sources), the samples follow tile by tile at the profile's
  network rate, the visible range first.

The device's time comes from ``clock`` (seconds): in the fast mode of a flow that is the virtual
time of the engine and captures complete at once; otherwise it is real time and a capture takes
as long as on a real device.

A simulator knows when it took its samples (:attr:`SimulatedDriver.knows_time`, the flows use it)
- unless its profile emulates USB (``usb``: ``frame``, ``latency``, ``jitter`` in seconds): then
blocks of a stream arrive at the next USB frame after their last sample, late and uneven, a capture
after its transfer (``stream_bandwidth``), and the time has to be found as with a real device
(``docs/timing.md``). With ``ring`` (bytes) the device streams from a ring of that size, as the Pico
firmware does: a host that reads later than the ring lasts ends the stream with an overflow.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Callable, Optional, Sequence

import numpy as np

from ...core import simulation
from ...core.overview import TILE_SAMPLES, Overview, ProgressiveCapture
from ..base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_ANALOG,
    CAPABILITY_PROGRESSIVE,
    CAPABILITY_RESTART,
    CAPABILITY_SIMULATION,
    CAPABILITY_STATE_MODE,
    CAPABILITY_STREAM_STATE,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    CaptureProgressArgs,
    CaptureTileArgs,
    DeviceSection,
    stream_sample_bytes,
)
from ..models import AnalogChannel, CaptureSession, EdgeKind, TriggerType
from .circuit import AnalogNoise, Circuit

#: Seconds of signal searched for a trigger before the capture gives up
TRIGGER_SEARCH_LIMIT = 10.0
#: Duration of a stream block (seconds of device time)
STREAM_BLOCK = 0.01
#: Seconds between two looks at the signal while waiting for a trigger in real time
SEARCH_INTERVAL = 0.002
#: Samples searched per step while waiting for a trigger
SEARCH_CHUNK = 1 << 18
#: virtual time: how long (real seconds) a capture waits for what the flow does right after it
#: started (a pulse when the capture is armed) before it reads the signal
SETTLE = 0.02
#: Overview blocks of a progressive capture at most (the base block grows with the capture)
OVERVIEW_BLOCKS = 1 << 16
#: Captures shorter than this many tiles are not transferred progressively
PROGRESSIVE_MIN_TILES = 4


#: samples of the time grid sampled at once for the states of a state capture
STATE_GRID = 1 << 20


class _Growing:
    """Samples arriving block by block, as one array: appending costs the block, not everything
    so far. ``keep``: only the newest ``keep`` samples (an endless stream). A view handed out
    stays valid: the buffer behind it is never written again once the data move."""

    def __init__(self, dtype, keep: Optional[int] = None) -> None:
        self.keep = keep
        self._data = np.zeros(4096, dtype=dtype)
        self._first = 0
        self._size = 0

    def append(self, values: np.ndarray) -> None:
        count = len(values)
        if self._size + count > len(self._data):
            kept = self.view()
            fresh = np.zeros(max(2 * (len(kept) + count), 4096), dtype=self._data.dtype)
            fresh[:len(kept)] = kept
            self._data, self._first, self._size = fresh, 0, len(kept)
        self._data[self._size:self._size + count] = values
        self._size += count
        if self.keep is not None and self._size - self._first > self.keep:
            self._first = self._size - self.keep

    def view(self) -> np.ndarray:
        return self._data[self._first:self._size]


class SimulatedDriver(AnalyzerDriverBase):
    """The capture side of a simulated instrument."""

    def __init__(self, profile: dict, circuit: Circuit, clock: Optional[Callable[[], float]] = None,
                 fast: bool = False) -> None:
        super().__init__()
        self.profile = profile
        self.circuit = circuit
        self.fast = fast
        #: random start value of the noise (the same seed gives the same samples)
        self.seed = 1
        start = time.monotonic()
        self.clock: Callable[[], float] = clock or (lambda: time.monotonic() - start)
        #: the sample clock of the device runs this much fast (``clock_drift`` of the profile, ppm): its
        #: times (starts, blocks) are on its own clock, the circuit is sampled - and the blocks arrive -
        #: on the true one
        self.drift = float(profile.get("clock_drift", 0.0)) * 1e-6
        self._drift_origin = self.clock()
        self._capturing = False
        self._abort = threading.Event()
        #: of the transfer of a progressive capture (see _start_transfer)
        self._transfer_abort = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: one capture at a time, also when a flow and the window start one at the same moment
        self._start_lock = threading.Lock()
        #: the generator facet, when the profile has outputs (set by ``open_simulated``)
        self.generator = None
        #: device time at the end of the last capture (the flow waits until then in virtual time)
        self.last_capture_end = 0.0
        #: device time of the trigger of the last capture
        self.last_trigger_time = 0.0
        #: device time of the first sample of the last stream
        self.last_stream_start = 0.0
        self._usb_random = random.Random(11)
        #: injected fault: None, "disconnect"
        self.fault: Optional[str] = None
        #: counts every change of an output (pin, PWM, pulse, DAC, generator): a capture in virtual
        #: time reads the signal again when the outputs changed after it started
        self.output_version = 0
        #: extra delay of every answer and command (fault injection), seconds
        self.delay = 0.0
        #: the next stream overflows (fault injection)
        self.force_overflow = False
        #: what the device did: (device time, text)
        self.events: list[tuple[float, str]] = []
        self._event_listeners: list[Callable[[float, str], None]] = []
        #: the GPIO facet, when the profile has one (set by ``open_simulated``)
        self.gpio = None
        #: the analog outputs (DAC), when the profile has them (set by ``open_simulated``)
        self.analog_out = None
        #: wires beyond those of the profile (``simulation.wiring`` of a project)
        self.extra_wiring: list = []
        #: what the device simulates (:mod:`.scenarios`), and the names it suggests for channels
        self.signals: dict = {"scenario": "default", "channels": {}}
        self.signal_names: dict[int, str] = {}
        #: nets that follow a net of another simulator (a wire between simulators, :meth:`follow`):
        #: net -> (the source that reads the other net, what drove the net before)
        self.followed: dict[str, tuple] = {}
        #: profile, boards and instance (set by ``open_simulated``)
        self.sim_address = None
        self._capture_channels: list = []

    # ------------------------------------------------------------- identity
    @property
    def device_version(self) -> Optional[str]:
        return f"openSciLab {self.profile['title']}"

    @property
    def address(self) -> Optional[str]:
        return str(self.sim_address) if self.sim_address is not None else f"sim:{self.profile['name']}"

    @property
    def driver_id(self) -> str:
        # names files (the capture settings of this kind of device): no "*" of a multi device
        return "sim-" + str(self.profile["name"]).replace("*", "-x")

    @property
    def board_count(self) -> int:
        return int(self.profile.get("boards", 1))

    @property
    def channels_per_device(self) -> int:
        return int(self.profile.get("channels_per_board", self.channel_count))

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.OTHER

    @property
    def max_frequency(self) -> int:
        return int(self.profile["max_rate"])

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def channel_count(self) -> int:
        return len(self.profile["digital"])

    @property
    def buffer_size(self) -> int:
        return int(self.profile["memory_depth"])

    @property
    def is_network(self) -> bool:
        return False

    @property
    def is_hardware(self) -> bool:
        # Behaves like a device: the analyzer captures with it.
        return True

    #: a simulator (behaves like a device, but nothing is connected to it unless the circuit says so)
    is_simulator = True

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def capabilities(self) -> frozenset[str]:
        capabilities = set(self.profile.get("capabilities", []))
        if self.profile.get("analog"):
            capabilities.add(f"{CAPABILITY_ANALOG}{len(self.profile['analog'])}")
        if self.profile.get("progressive"):
            capabilities.add(CAPABILITY_PROGRESSIVE)
        capabilities.add(CAPABILITY_RESTART)
        return frozenset(capabilities)

    def channel_names(self) -> list[str]:
        return list(self.profile["digital"])

    @property
    def analog_channel_count(self) -> int:
        return len(self.profile.get("analog", []))

    def analog_channel_names(self) -> list[str]:
        return list(self.profile.get("analog", []))

    def memory_depth(self, digital: int, analog: int) -> Optional[int]:
        """Samples per channel with ``digital`` and ``analog`` channels in use (profile
        ``memory_depth_analog`` / ``memory_depth_digital``: depth by the number of channels)."""
        for count, name in ((analog, "memory_depth_analog"), (digital, "memory_depth_digital")):
            table = self.profile.get(name) or {}
            if count and table:
                keys = sorted(int(key) for key in table)
                key = next((candidate for candidate in keys if candidate >= count), keys[-1])
                return int(table[str(key)] if str(key) in table else table[key])
        return int(self.profile["memory_depth"])

    def state_clock_channels(self) -> list[int]:
        """Channels that can clock the state mode (pins with ``CLOCK``/``CLOCK_SW``)."""
        if CAPABILITY_STATE_MODE not in self.capabilities():
            return []
        names = self.channel_names()
        clocks = self.profile.get("clock_pins")
        if clocks is None:
            return list(range(len(names)))
        return [names.index(name) for name in clocks if name in names]

    def supports_state_mode(self) -> bool:
        return CAPABILITY_STATE_MODE in self.capabilities()

    @property
    def state_max_clock(self) -> int:
        return int(self.profile.get("state_max_clock", self.max_frequency // 4))
    def describe(self) -> list[DeviceSection]:
        rows = [("Profile", self.profile["name"]), ("Channels", str(self.channel_count)),
                ("Maximum rate", f"{self.max_frequency:,} Hz"), ("Memory depth", f"{self.buffer_size:,} samples"),
                ("Stream", f"{int(self.profile['stream_bandwidth']):,} bytes/s"),
                ("Time", "virtual (fast)" if self.fast else "real time")]
        circuit = [(net, text) for net, text in self.circuit.describe().items()]
        return [("Simulated device", rows), ("Circuit", circuit or [("Inputs", "not connected")])]

    def device_details(self) -> dict[str, str]:
        return {"Profile": self.profile["name"]}

    # ---------------------------------------------------------------- limits
    def acquisition_modes(self) -> tuple[str, ...]:
        # (an instrument without a link fast enough to stream - a scope - only captures)
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM) if self.profile.get("stream_bandwidth") else (ACQUISITION_BUFFER,)

    def stream_rate_limit(self, channels: Sequence[int], analog: int = 0) -> int:
        """The highest stream rate the link carries for ``channels`` and ``analog`` analog inputs (2 bytes
        a sample each)."""
        width = (self.get_capture_mode(channels or [0]).bytes_per_sample if channels or not analog else 0) + 2 * analog
        bandwidth = float(self.profile.get("stream_bandwidth") or 0)
        limit = min(bandwidth // max(width, 1), self.max_frequency, self.profile.get("stream_max_rate") or math.inf)
        if analog and self.profile.get("analog_aggregate_rate"):
            limit = min(limit, float(self.profile["analog_aggregate_rate"]) / analog)
        return int(limit)

    def max_frequency_for(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> int:
        if acquisition_mode == ACQUISITION_STREAM:
            return self.stream_rate_limit(channels)
        return self.max_frequency

    def get_limits(self, channels, acquisition_mode=None, *, to_disk=False, continuous=False,
                   analog: int = 0) -> CaptureLimits:
        if acquisition_mode == ACQUISITION_STREAM:
            total = max(stream_sample_bytes(to_disk, continuous) // max(len(list(channels)), 1), 1)
            return CaptureLimits(0, 0, 1, total)
        depth = self.memory_depth(len(list(channels)), analog) or self.buffer_size
        return CaptureLimits(min_pre_samples=0, max_pre_samples=depth // 2, min_post_samples=1,
                             max_post_samples=depth)

    def edge_trigger_channels(self) -> list[int]:
        return list(range(self.channel_count))

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        return ((0, self.channel_count - 1),)

    # -------------------------------------------------------------- sampling
    def true_time(self, at: float) -> float:
        """A time of the device's sample clock on the true clock (the circuit's)."""
        if not self.drift:
            return at
        return self._drift_origin + (at - self._drift_origin) / (1.0 + self.drift)

    def set_drift(self, ppm: float) -> None:
        """Let the sample clock run ``ppm`` fast from now on (no jump: it agrees with the true clock now)."""
        now = self.clock()
        self._drift_origin = now
        self.drift = float(ppm) * 1e-6

    def sample(self, numbers: Sequence[int], start: float, rate: float, count: int) -> dict[int, np.ndarray]:
        names = self.channel_names()
        start, rate = self.true_time(start), rate * (1.0 + self.drift)
        return {number: self.circuit.digital(names[number], start, rate, count) for number in numbers}

    # ------------------------------------------------------- events, faults
    def outputs_changed(self) -> None:
        self.output_version += 1

    def device_time(self, at: float) -> float:
        """A time of the true clock (the circuit's, the flow's) on the device's sample clock."""
        if not self.drift:
            return at
        return self._drift_origin + (at - self._drift_origin) * (1.0 + self.drift)

    def release_outputs(self, at: Optional[float] = None) -> None:
        """Generators stop and analog outputs go to 0 V (*Safe*, a restart, the watchdog); the pins
        are the GPIO facet's."""
        if self.generator is not None:
            self.generator.stop_all(at)
        if self.analog_out is not None:
            self.analog_out.release(at)

    def log(self, text: str) -> None:
        stamp = self.clock()
        self.events.append((stamp, text))
        del self.events[:-1000]
        for listener in list(self._event_listeners):
            listener(stamp, text)

    def add_event_listener(self, listener: Callable[[float, str], None]) -> None:
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[float, str], None]) -> None:
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    def inject(self, kind: str, value: float = 0.0) -> None:
        """Fault injection: ``disconnect``, ``reconnect``, ``delay`` (seconds), ``overflow`` (the
        next stream), ``restart`` (outputs released, the device answers again)."""
        if kind == "disconnect":
            self.fault = "disconnect"
            self._abort.set()
        elif kind == "reconnect":
            self.fault = None
        elif kind == "delay":
            self.delay = max(float(value), 0.0)
        elif kind == "overflow":
            self.force_overflow = True
        elif kind == "restart":
            self.fault = None
            self.delay = 0.0
            if self.gpio is not None:
                self.gpio.restart()
            else:
                self.release_outputs()
        else:
            raise ValueError(f"unknown fault {kind!r}")
        self.log(f"fault: {kind}" + (f" {value:g}" if value else ""))

    # ---------------------------------------------------------------- analog
    def analog_scale(self) -> tuple[float, float, int]:
        """``(scale, offset, bits)`` of the ADC: volts per count and volts at count 0."""
        low, high = self.profile.get("analog_range", [-5.0, 5.0])
        bits = int(self.profile.get("adc_bits", 12))
        return (float(high) - float(low)) / (1 << bits), (float(high) + float(low)) / 2.0, bits

    def sample_analog(self, number: int, start: float, rate: float, count: int) -> np.ndarray:
        """Raw ``int16`` counts of analog input ``number`` (with the ADC's noise and resolution)."""
        names = self.analog_channel_names()
        scale, offset, bits = self.analog_scale()
        start, rate = self.true_time(start), rate * (1.0 + self.drift)
        volts = self.circuit.analog(names[number], start, rate, count)
        noise = float(self.profile.get("adc_noise", 0.0))
        if noise:
            volts = volts + AnalogNoise(rms=noise, bandwidth=rate, seed=1000 * self.seed + number).analog(start, rate, count)
        limit = 1 << (bits - 1)
        return np.clip(np.round((volts - offset) / scale), -limit, limit - 1).astype(np.int16)

    def analog_channels_for(self, session: CaptureSession) -> list[AnalogChannel]:
        scale, offset, _bits = self.analog_scale()
        names = self.analog_channel_names()
        for channel in session.analog_channels:
            if not 0 <= channel.channel_number < len(names):
                raise ValueError(f"no analog input {channel.channel_number}")
            channel.scale, channel.offset, channel.unit = scale, offset, "V"
            channel.channel_name = channel.channel_name or names[channel.channel_number]
            channel.rate = None
        return session.analog_channels
    def _find_trigger(self, session: CaptureSession, start: float, end: Optional[float] = None) -> Optional[float]:
        """Device time of the first trigger at or after ``start`` (and before ``end``), ``None`` within
        the limit."""
        rate = float(session.frequency)
        trigger = session.trigger_type
        if trigger in (TriggerType.IMMEDIATE, TriggerType.SIMULATION):
            return start
        searched = 0
        limit = int(TRIGGER_SEARCH_LIMIT * rate) if end is None else max(int(round((end - start) * rate)), 0)
        while searched < limit:
            if self._abort.is_set():
                return None  # stopped: at once, not at the end of the search
            count = min(SEARCH_CHUNK, limit - searched)
            chunk_start = start + searched / rate
            if not self.fast:
                # Real time: only the signal up to now exists (a stimulus may still come). It is
                # searched a few hundred times a second, not sample by sample as fast as possible.
                available = int((self.clock() - chunk_start) * rate)
                if available < max(int(rate * SEARCH_INTERVAL), 1):
                    if self._abort.wait(SEARCH_INTERVAL):
                        return None
                    available = int((self.clock() - chunk_start) * rate)
                    if available < 1:
                        continue
                count = min(count, available)
            if trigger in (TriggerType.EDGE, TriggerType.EDGE_OUT):
                # with the sample before the chunk: an edge right at its first sample counts too
                levels = self.sample([session.trigger_channel], chunk_start - 1.0 / rate, rate,
                                     count + 1)[session.trigger_channel]
                wanted = (levels[:-1] == 1) & (levels[1:] == 0) if session.trigger_inverted else (
                    (levels[:-1] == 0) & (levels[1:] == 1))
                hits = np.flatnonzero(wanted)
                if hits.size:
                    return chunk_start + hits[0] / rate
            elif trigger in (TriggerType.COMPLEX, TriggerType.FAST):
                bits = max(session.trigger_bit_count, 1)
                channels = list(range(session.trigger_channel, session.trigger_channel + bits))
                samples = self.sample(channels, chunk_start, rate, count)
                match = np.ones(count, dtype=bool)
                mask = session.trigger_mask
                for offset, channel in enumerate(channels):
                    if mask is not None and not (mask >> offset) & 1:
                        continue  # (don't care)
                    match &= samples[channel] == ((session.trigger_pattern >> offset) & 1)
                hits = np.flatnonzero(match)
                if hits.size:
                    return chunk_start + hits[0] / rate
            else:
                return None
            searched += count
        return None

    # ------------------------------------------------------------------ time
    @property
    def knows_time(self) -> bool:
        """Whether the flows may use the simulator's own time of its samples (not when it emulates
        USB: then they find it as with a real device)."""
        return not self.profile.get("usb")

    def _delivered(self, ready: float, size: int = 0) -> float:
        """When something ready at ``ready`` (device time) arrives over the emulated USB: at the next
        frame, after the latency, a random jitter and the transfer of ``size`` bytes."""
        ready = self.true_time(ready)
        usb = self.profile.get("usb")
        if not usb:
            return ready
        frame = float(usb.get("frame", 0.001))
        transfer = size / float(self.profile.get("stream_bandwidth") or 1e9)
        return (math.ceil((ready + transfer) / frame) * frame + float(usb.get("latency", 0.001))
                + self._usb_random.expovariate(1.0 / max(float(usb.get("jitter", 0.0003)), 1e-9)))

    # --------------------------------------------------------------- capture
    def start_capture(self, session: CaptureSession,
                      completed_handler: Optional[CaptureCompletedHandler] = None) -> CaptureError:
        with self._start_lock:
            return self._start_capture(session, completed_handler)

    def _start_capture(self, session: CaptureSession,
                       completed_handler: Optional[CaptureCompletedHandler] = None) -> CaptureError:
        if self._capturing and self._thread is not None and not self._thread.is_alive():
            self._capturing = False  # its thread is gone (it failed): the device is free again
        if self._capturing:
            return CaptureError.BUSY
        self._capture_channels = list(session.capture_channels)
        error = self.check_capture(session)
        if error != CaptureError.NONE:
            return error
        if session.clock_channel is not None:
            return self._start_state(session, completed_handler)
        if session.acquisition_mode == ACQUISITION_STREAM:
            return self._start_stream(session, completed_handler)
        self._capturing = True
        self._abort.clear()
        start = self.clock() + float(self.profile.get("latency", 0.0))
        self._thread = threading.Thread(target=self._buffer, args=(session, start, completed_handler),
                                        name="openscilab-sim-capture", daemon=True)
        self._thread.start()
        return CaptureError.NONE

    def check_capture(self, session: CaptureSession) -> CaptureError:
        """What :meth:`start_capture` answers to ``session`` (a capture or a stream; of a state capture
        only its channels), without starting it."""
        if self.fault == "disconnect":
            return CaptureError.HARDWARE_ERROR
        numbers = session.channel_numbers
        analog = session.analog_channels
        if (not numbers and not analog) or (numbers and (min(numbers) < 0 or max(numbers) >= self.channel_count)) \
                or session.frequency <= 0:
            return CaptureError.BAD_PARAMS
        try:
            self.analog_channels_for(session)
        except ValueError:
            return CaptureError.BAD_PARAMS
        if session.clock_channel is not None:
            return CaptureError.NONE
        if session.acquisition_mode == ACQUISITION_STREAM:
            limits = self.get_limits(numbers, ACQUISITION_STREAM, to_disk=session.to_disk,
                                     continuous=session.continuous)
            if (session.trigger_type != TriggerType.IMMEDIATE or session.loop_count
                    or not 1 <= session.post_trigger_samples <= limits.max_post_samples):
                return CaptureError.BAD_PARAMS
            if session.frequency > self.stream_rate_limit(numbers, len(analog)):
                return CaptureError.BAD_PARAMS
            return CaptureError.NONE
        if session.frequency > self.max_frequency or session.loop_count:
            return CaptureError.BAD_PARAMS
        if analog and self.profile.get("buffer_analog") is False:
            return CaptureError.UNSUPPORTED  # (its buffer captures read the pins only)
        if analog and self.profile.get("analog_aggregate_rate") and \
                session.frequency * len(analog) > float(self.profile["analog_aggregate_rate"]):
            return CaptureError.BAD_PARAMS
        total = session.pre_trigger_samples + session.post_trigger_samples
        if total <= 0 or total > (self.memory_depth(len(numbers), len(analog)) or self.buffer_size):
            return CaptureError.BAD_PARAMS
        if self.profile.get("max_post_seconds") and \
                session.post_trigger_samples > float(self.profile["max_post_seconds"]) * session.frequency:
            return CaptureError.BAD_PARAMS
        if session.trigger_type not in (TriggerType.EDGE, TriggerType.EDGE_OUT, TriggerType.IMMEDIATE,
                                        TriggerType.COMPLEX, TriggerType.FAST, TriggerType.SIMULATION):
            return CaptureError.BAD_PARAMS
        if session.trigger_type == TriggerType.SIMULATION and CAPABILITY_SIMULATION not in self.capabilities():
            return CaptureError.BAD_PARAMS
        return CaptureError.NONE

    # ------------------------------------------------------- virtual time
    # A flow in virtual time captures without the device's thread: it lets its own time run on while
    # the capture waits for its trigger and records, and takes the samples afterwards - with what the
    # flow did meanwhile (a stimulus after 'armed', after a wait; see the device nodes).
    def virtual_start(self, session: CaptureSession) -> float:
        """A capture or stream of ``session`` begins (the device is busy until :meth:`virtual_end`);
        returns the device time it begins at."""
        self._capturing = True
        self._capture_channels = list(session.capture_channels)
        self._abort.clear()
        return self.clock() + float(self.profile.get("latency", 0.0))

    def virtual_end(self) -> None:
        self._capturing = False
        self._capture_channels = []

    def find_trigger(self, session: CaptureSession, start: float, end: float) -> Optional[float]:
        """The first trigger of ``session`` from ``start`` to ``end`` (device time)."""
        return self._find_trigger(session, start, end)

    def take(self, session: CaptureSession, first: float) -> None:
        """The samples of the capture ``session`` from device time ``first`` on."""
        rate = float(session.frequency)
        pre, post = session.pre_trigger_samples, session.post_trigger_samples
        if session.trigger_type == TriggerType.SIMULATION:
            levels = simulation.generate(session.trigger_pattern, len(session.capture_channels), pre + post)
            for channel, samples in zip(session.capture_channels, levels):
                channel.samples = samples
        else:
            samples = self.sample(session.channel_numbers, first, rate, pre + post)
            for channel in session.capture_channels:
                channel.samples = samples[channel.channel_number]
        for channel in session.analog_channels:
            channel.raw = self.sample_analog(channel.channel_number, first, rate, pre + post)
            channel.length = None
        session.bursts = None
        self.last_trigger_time = first + pre / rate
        self.last_capture_end = first + (pre + post) / rate

    def take_block(self, session: CaptureSession, start: float, count: int) -> tuple[dict, dict]:
        """``count`` samples of the stream ``session`` from ``start``: digital levels and raw analog
        counts by channel number."""
        rate = float(session.frequency)
        digital = self.sample(session.channel_numbers, start, rate, count)
        analog = {channel.channel_number: self.sample_analog(channel.channel_number, start, rate, count)
                  for channel in session.analog_channels}
        self.last_capture_end = start + count / rate
        return digital, analog

    def take_overflow(self) -> bool:
        """Whether the next stream overflows (fault injection); once."""
        overflow, self.force_overflow = self.force_overflow, False
        return overflow

    def _buffer(self, session: CaptureSession, start: float, handler) -> None:
        try:
            self._buffer_capture(session, start, handler)
        except Exception as error:  # noqa: BLE001 - reported to the user; the device stays usable
            self._finish(session, handler, False, f"The simulator failed: {error}")

    def _buffer_capture(self, session: CaptureSession, start: float, handler) -> None:
        rate = float(session.frequency)
        pre, post = session.pre_trigger_samples, session.post_trigger_samples
        if session.trigger_type == TriggerType.SIMULATION:
            levels = simulation.generate(session.trigger_pattern, len(session.capture_channels), pre + post)
            for channel, samples in zip(session.capture_channels, levels):
                channel.samples = samples
            trigger = start
        else:
            version = self.output_version
            trigger = self._find_trigger(session, start + pre / rate)
            while self.fast:
                # Virtual time: the device sees the whole signal at once, also what the flow
                # changes only after the capture started (a pulse on 'armed'). Outputs that change
                # before the flow goes on make the device look again, as the real one would see them.
                if self._abort.wait(SETTLE) or version == self.output_version:
                    break
                version = self.output_version
                trigger = self._find_trigger(session, start + pre / rate)
            if trigger is None and self._abort.is_set():
                self._capturing = False
                return
            if trigger is None:
                self._finish(session, handler, False, "No trigger within "
                             f"{TRIGGER_SEARCH_LIMIT:g} s of signal.")
                return
            first = trigger - pre / rate
            if self._progressive(session, pre + post):
                self._start_progressive(session, first, rate, handler)
                end = trigger + post / rate
                self.last_trigger_time, self.last_capture_end = trigger, end
                return
            if not self.fast:
                # Real time: the samples are what the signal does until the capture ends - outputs
                # switched while it runs are in it, as on the real device
                end = self.true_time(trigger + post / rate)
                while self.clock() < end and not self._abort.is_set():
                    time.sleep(min(0.01, max(end - self.clock(), 0)))
                if self._abort.is_set():
                    self._capturing = False
                    return
            samples = self.sample(session.channel_numbers, first, rate, pre + post)
            for channel in session.capture_channels:
                channel.samples = samples[channel.channel_number]
            for channel in session.analog_channels:
                channel.raw = self.sample_analog(channel.channel_number, first, rate, pre + post)
                channel.length = None
        end = trigger + post / rate
        self.last_trigger_time = trigger
        self.last_capture_end = end
        if not self.fast:
            # Real time: the capture takes as long as the signal it records (and its transfer).
            size = (pre + post) * max(len(session.capture_channels), 1) // 8 + 2 * (pre + post) * len(session.analog_channels)
            arrive = self._delivered(end, size)
            while self.clock() < arrive and not self._abort.is_set():
                time.sleep(min(0.01, max(arrive - self.clock(), 0)))
            if self._abort.is_set():
                self._capturing = False
                return
        session.bursts = None
        self._finish(session, handler, True)

    def _finish(self, session, handler, success: bool, error: Optional[str] = None, first: int = 0) -> None:
        self._capturing = False
        self._capture_channels = []
        self._raise_capture_completed(CaptureCompletedArgs(success=success, session=session, error=error,
                                                           first_sample=first), handler)

    def _start_stream(self, session: CaptureSession, handler) -> CaptureError:
        self._capturing = True
        self._abort.clear()
        start = self.clock() + float(self.profile.get("latency", 0.0))
        self.last_stream_start = start
        self._thread = threading.Thread(target=self._stream, args=(session, start, handler),
                                        name="openscilab-sim-stream", daemon=True)
        self._thread.start()
        return CaptureError.NONE

    def _stream(self, session: CaptureSession, start: float, handler) -> None:
        from ...core.sample_store import DiskAllocator, MemoryAllocator, RingStore, SampleStore

        rate = float(session.frequency)
        numbers = session.channel_numbers
        wanted = session.post_trigger_samples
        allocator = DiskAllocator() if session.to_disk else MemoryAllocator()
        store = RingStore(numbers, wanted, allocator) if session.continuous else SampleStore(numbers, wanted, allocator)
        block = max(int(rate * STREAM_BLOCK), 1)
        produced = 0
        analog_parts = {channel.channel_number: _Growing(np.int16, wanted if session.continuous else None)
                        for channel in session.analog_channels}
        overflow = self.force_overflow
        self.force_overflow = False
        # the device's ring (bytes) and the bytes of a sample of these channels, as the Pico sends them
        ring = int((self.profile.get("usb") or {}).get("ring", 0))
        width = 1 if len(numbers) <= 8 else 2 if len(numbers) <= 16 else 4
        try:
            while session.continuous or produced < wanted:
                if self._abort.is_set():
                    break
                if overflow and produced >= max(wanted // 3, 1):
                    break
                count = block if session.continuous else min(block, wanted - produced)
                block_start = start + produced / rate
                if not self.fast:
                    # A real device delivers a block once its samples were taken (over USB: later).
                    ready = self._delivered(block_start + count / rate)
                    while self.clock() < ready and not self._abort.is_set():
                        time.sleep(min(0.005, max(ready - self.clock(), 0)))
                    if self._abort.is_set():
                        break
                    if ring and (self.clock() - ready) * rate * width > ring:
                        overflow = True  # this host read too late: the device's ring ran over
                        break
                store.append(self.sample(numbers, block_start, rate, count))
                for channel in session.analog_channels:
                    analog_parts[channel.channel_number].append(
                        self.sample_analog(channel.channel_number, block_start, rate, count))
                produced += count
                views, first = store.window()
                analog_views = {number: parts.view() for number, parts in analog_parts.items()}
                self._raise_capture_progress(CaptureProgressArgs(session, views, first, analog=analog_views))
                if self.fast and session.continuous and produced >= wanted * 4:
                    break  # a fast flow stops an endless stream itself; never run away
        except Exception as error:  # noqa: BLE001 - reported to the user
            self._finish(session, handler, False, str(error))
            return
        samples, first = store.result()
        count = min((len(values) for values in samples.values()), default=0)
        if not numbers:  # only analog channels: as many samples as they have
            count = min((len(parts.view()) for parts in analog_parts.values()), default=0)
        for channel in session.capture_channels:
            channel.samples = samples[channel.channel_number][:count]
        for channel in session.analog_channels:
            values = analog_parts[channel.channel_number].view()
            channel.raw = (values[-count:] if count else values[:0]).copy()
            channel.length = None
        session.pre_trigger_samples = 0
        session.post_trigger_samples = count
        session.bursts = None
        self.last_trigger_time = start
        self.last_capture_end = start + produced / rate
        error = ("The device could not keep up with the stream, so it stopped early (overflow)."
                 if overflow else None)
        self._finish(session, handler, count > 0, error, first)

    # ------------------------------------------------------------ progressive
    def _progressive(self, session: CaptureSession, total: int) -> bool:
        options = self.profile.get("progressive")
        if not options:
            return False
        tile = int(options.get("tile", TILE_SAMPLES))
        return total >= tile * PROGRESSIVE_MIN_TILES

    def _start_progressive(self, session: CaptureSession, first_time: float, rate: float, handler) -> None:
        """Complete at once with overviews, then transfer the tiles in a thread."""
        from ...core.sample_store import DiskAllocator

        options = self.profile["progressive"]
        total = session.pre_trigger_samples + session.post_trigger_samples
        allocator = DiskAllocator()
        base = 16
        while (total + base - 1) // base > OVERVIEW_BLOCKS:
            base *= 16
        progressive = ProgressiveCapture(total, int(options.get("tile", TILE_SAMPLES)))
        names = self.channel_names()
        for channel in session.capture_channels:
            channel.samples = allocator.zeros(total, np.uint8)
            mins, maxs = self.circuit.envelope(names[channel.channel_number], first_time, rate, total, base)
            progressive.overviews[("d", channel.channel_number)] = Overview.from_blocks(
                mins.astype(np.uint8), maxs.astype(np.uint8), total, base)
        scale, offset, bits = self.analog_scale()
        limit = 1 << (bits - 1)
        analog_names = self.analog_channel_names()
        for channel in session.analog_channels:
            channel.raw = allocator.zeros(total, np.int16)
            channel.length = total
            low, high = self.circuit.envelope(analog_names[channel.channel_number], first_time, rate, total, base,
                                              analog=True)
            noise = 3.5 * float(self.profile.get("adc_noise", 0.0))
            to_raw = lambda volts: np.clip(np.round((volts - offset) / scale), -limit, limit - 1).astype(np.int16)  # noqa: E731
            progressive.overviews[("a", channel.channel_number)] = Overview.from_blocks(
                to_raw(low - noise), to_raw(high + noise), total, base)
        session.progressive = progressive
        session.bursts = None
        self._transfer = (session, first_time, rate)
        self._capturing = False
        self._raise_capture_completed(CaptureCompletedArgs(success=True, session=session), handler)
        self._start_transfer()

    def _start_transfer(self) -> None:
        # a flag of its own: stopping a capture does not interrupt the transfer of the capture
        # before it, and stopping the transfer does not end a capture
        self._transfer_abort = abort = threading.Event()
        self._transfer_thread = threading.Thread(target=self._transfer_tiles, args=(abort,),
                                                 name="openscilab-sim-transfer", daemon=True)
        self._transfer_thread.start()

    def _transfer_tiles(self, abort: threading.Event) -> None:
        session, first_time, rate = self._transfer
        progressive: ProgressiveCapture = session.progressive
        options = self.profile["progressive"]
        bytes_per_sample = max((len(session.capture_channels) + 7) // 8, 1) + 2 * len(session.analog_channels)
        lan_rate = float(options.get("lan_rate", 0) or 0)
        names = self.channel_names()
        started = time.monotonic()
        sent = 0
        while True:
            tile = progressive.next_tile()
            if tile is None:
                break
            if abort.is_set():
                progressive.interrupted = True
                self._raise_capture_tile(CaptureTileArgs(session, 0, 0, False, "the transfer was interrupted"))
                return
            start, end = progressive.tile_range(tile)
            count = end - start
            tile_time = first_time + start / rate
            for channel in session.capture_channels:
                channel.samples[start:end] = self.circuit.digital(names[channel.channel_number], tile_time, rate, count)
            for channel in session.analog_channels:
                channel.raw[start:end] = self.sample_analog(channel.channel_number, tile_time, rate, count)
            sent += count * bytes_per_sample
            if lan_rate and not self.fast:
                wait = sent / lan_rate - (time.monotonic() - started)
                if wait > 0 and abort.wait(wait):
                    progressive.interrupted = True
                    self._raise_capture_tile(CaptureTileArgs(session, 0, 0, False, "the transfer was interrupted"))
                    return
            progressive.mark_loaded(tile)
            self._raise_capture_tile(CaptureTileArgs(session, start, count, progressive.complete))
        progressive.interrupted = False

    def resume_transfer(self, session: CaptureSession) -> bool:
        transfer = getattr(self, "_transfer", None)
        if transfer is None or transfer[0] is not session or session.progressive is None:
            return False
        if session.progressive.complete:
            return True
        self._start_transfer()
        return True

    def stop_transfer(self) -> None:
        self._transfer_abort.set()

    # ------------------------------------------------------------------ state
    def _clock_edges(self, session: CaptureSession, start: float, count: int, resolution: float) -> np.ndarray:
        """Times of the next ``count`` active edges of the clock channel from ``start``."""
        name = self.channel_names()[session.clock_channel]
        found: list[np.ndarray] = []
        total = 0
        position = start
        limit = start + TRIGGER_SEARCH_LIMIT
        chunk = SEARCH_CHUNK
        previous: Optional[int] = None
        while total < count and position < limit:
            levels = self.circuit.digital(name, position, resolution, chunk)
            if previous is not None:
                levels = np.concatenate([[previous], levels])
                base = position - 1 / resolution
            else:
                base = position
            values = levels.astype(np.int8)
            if session.clock_edge == EdgeKind.FALLING:
                hits = np.flatnonzero((values[:-1] == 1) & (values[1:] == 0)) + 1
            elif session.clock_edge == EdgeKind.RISING:
                hits = np.flatnonzero((values[:-1] == 0) & (values[1:] == 1)) + 1
            else:
                hits = np.flatnonzero(values[:-1] != values[1:]) + 1
            times = base + hits / resolution
            found.append(times[: count - total])
            total += min(len(times), count - total)
            previous = int(levels[-1])
            position += chunk / resolution
        return np.concatenate(found) if found else np.zeros(0)

    def _sample_states(self, session: CaptureSession, times: np.ndarray, resolution: float):
        """Levels of the channels and analog inputs just after each clock edge."""
        names = self.channel_names()
        delay = 0.5 / resolution
        # The edges lie on the grid of ``resolution``: every net is sampled once for a run of
        # edges close together (at most STATE_GRID grid points) and the states are picked from
        # it – not one call per state and channel.
        runs: list[tuple[int, int, np.ndarray, int]] = []  # first edge, end, grid index of each edge, grid points
        begin = 0
        while begin < len(times):
            index = np.rint((times[begin:] - times[begin]) * resolution).astype(np.int64)
            inside = int(np.searchsorted(index, STATE_GRID, side="left")) or 1
            runs.append((begin, begin + inside, index[:inside], int(index[inside - 1]) + 1))
            begin += inside
        digital = {}
        for channel in session.capture_channels:
            net = names[channel.channel_number]
            levels = np.zeros(len(times), dtype=np.uint8)
            for first, end, index, points in runs:
                levels[first:end] = self.circuit.digital(net, float(times[first]) + delay, resolution, points)[index]
            digital[channel.channel_number] = levels
        analog = {}
        for channel in session.analog_channels:
            values = np.zeros(len(times), dtype=np.int16)
            for first, end, index, points in runs:
                values[first:end] = self.sample_analog(channel.channel_number, float(times[first]) + delay,
                                                       resolution, points)[index]
            analog[channel.channel_number] = values
        return digital, analog

    def _start_state(self, session: CaptureSession, handler) -> CaptureError:
        if CAPABILITY_STATE_MODE not in self.capabilities():
            return CaptureError.UNSUPPORTED
        if session.clock_channel not in self.state_clock_channels():
            return CaptureError.BAD_PARAMS
        stream = session.acquisition_mode == ACQUISITION_STREAM
        if stream and CAPABILITY_STREAM_STATE not in self.capabilities():
            return CaptureError.UNSUPPORTED
        if session.trigger_type not in (TriggerType.IMMEDIATE, TriggerType.EDGE, TriggerType.COMPLEX):
            return CaptureError.BAD_PARAMS
        if session.post_trigger_samples < 1:
            return CaptureError.BAD_PARAMS
        self._capturing = True
        self._abort.clear()
        start = self.clock() + float(self.profile.get("latency", 0.0))
        self._thread = threading.Thread(target=self._state, args=(session, start, handler, stream),
                                        name="openscilab-sim-state", daemon=True)
        self._thread.start()
        return CaptureError.NONE

    def _state(self, session: CaptureSession, start: float, handler, stream: bool) -> None:
        resolution = float(min(max(session.frequency, 1), self.max_frequency))
        count = session.post_trigger_samples
        block = max(min(count // 10, 4096), 1) if stream else count
        all_times = _Growing(np.float64)
        digital = {channel.channel_number: _Growing(np.uint8) for channel in session.capture_channels}
        analog = {channel.channel_number: _Growing(np.int16) for channel in session.analog_channels}
        produced = 0
        position = self._find_trigger(session, start)  # (the first clock edge at or after the trigger)
        if position is None:
            if self._abort.is_set():
                self._capturing = False
            else:
                self._finish(session, handler, False, f"No trigger within {TRIGGER_SEARCH_LIMIT:g} s of signal.")
            return
        first_time: Optional[float] = None
        try:
            while produced < count and not self._abort.is_set():
                times = self._clock_edges(session, position, min(block, count - produced), resolution)
                if not len(times):
                    break
                if first_time is None:
                    first_time = float(times[0])
                levels, values = self._sample_states(session, times, resolution)
                for number, part in levels.items():
                    digital[number].append(part)
                for number, part in values.items():
                    analog[number].append(part)
                all_times.append(times)
                produced += len(times)
                position = float(times[-1]) + 0.5 / resolution
                if not self.fast:
                    while self.clock() < position and not self._abort.is_set():
                        time.sleep(min(0.005, max(position - self.clock(), 0)))
                if stream:
                    stamps = (all_times.view() - first_time) * 1e6
                    views = {number: parts.view() for number, parts in digital.items()}
                    analog_views = {number: parts.view() for number, parts in analog.items()}
                    self._raise_capture_progress(CaptureProgressArgs(session, views, 0, analog=analog_views,
                                                                     state_times=stamps))
        except Exception as error:  # noqa: BLE001 - reported to the user
            self._finish(session, handler, False, str(error))
            return
        if not produced:
            self._finish(session, handler, False, "The clock input did not change.")
            return
        for channel in session.capture_channels:
            channel.samples = digital[channel.channel_number].view().copy()
        for channel in session.analog_channels:
            channel.raw = analog[channel.channel_number].view().copy()
            channel.length = None
        times = all_times.view().copy()
        session.state_times = (times - times[0]) * 1e6
        session.pre_trigger_samples = 0
        session.post_trigger_samples = produced
        session.bursts = None
        self.last_trigger_time = float(times[0])
        self.last_capture_end = float(times[-1])
        self._finish(session, handler, True)

    def stop_capture(self) -> bool:
        if not self._capturing:
            return False
        self._abort.set()
        if self._thread is None or not self._thread.is_alive():
            self._capturing = False  # nothing left that could end the capture
        return True

    def enter_bootloader(self) -> bool:
        return False

    def restart(self) -> bool:
        """As after power-up: no fault, every pin an input again."""
        if self._capturing:
            return False
        self.fault = None
        self.delay = 0.0
        if self.gpio is not None:
            self.gpio.restart()
        self.log("restart")
        return True

    def follow(self, net: str, source) -> None:
        """``net`` follows a net of another simulator from now on (``source``: a
        :class:`.circuit.RemoteSource` or a :class:`.nets.NetSource`); it stays wired when the
        device simulates something else. :meth:`unfollow` gives the net back."""
        previous = self.followed[net][1] if net in self.followed else self.circuit.sources.get(net)
        old = self.followed.get(net, (None,))[0]
        self.followed[net] = (source, previous)
        self.circuit.drive(net, source)
        if old is not None and old is not source and hasattr(old, "close"):
            old.close()
        self.log(f"{net}: {source.describe()}")

    def unfollow(self, net: str) -> None:
        """``net`` is driven again by what drove it before :meth:`follow`."""
        if net not in self.followed:
            return
        source, previous = self.followed.pop(net)
        if self.circuit.sources.get(net) is source:
            if previous is None:
                self.circuit.sources.pop(net, None)
            else:
                self.circuit.sources[net] = previous
        if hasattr(source, "close"):
            source.close()
        self.log(f"{net}: no longer wired to another simulator")

    def dispose(self) -> None:
        self._abort.set()
        self._transfer_abort.set()
        self._event_listeners.clear()
        for net in list(self.followed):
            source, _previous = self.followed.pop(net)
            if hasattr(source, "close"):
                source.close()
        super().dispose()
