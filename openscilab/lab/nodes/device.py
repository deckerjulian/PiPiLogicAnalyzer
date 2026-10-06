# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Device nodes: the instruments of a flow, their captures and streams.

A ``device.instrument`` node stands for an instrument; its ``device`` output is wired to the
``device`` input of the nodes that use it (capture, stream, monitor, GPIO, generator).

The samples of a capture or stream are placed on the flow's time by the instrument's
:class:`~openscilab.core.timing.Acquisition`: a simulator knows its time; a real device is placed
between the command that started it and the arrival of its data (less a latency measured with
``timing.calibrate``), by a time scale it stamps in, or by a sync signal (``timing.align``).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import numpy as np

from ...core import signals
from ...core.instrument import CaptureFacet, Instrument
from ...core.timing import Acquisition, calibration_key, stored_latency, timing_of
from ...driver.base import ACQUISITION_STREAM, CaptureError
from ...driver.models import AnalyzerChannel, CaptureSession, EdgeKind, TriggerType
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, Out, Param, collect, node


def split_channels(instrument: Instrument, names) -> tuple[list, list[tuple[str, int]]]:
    """``names`` as digital names and ``(name, analog channel number)`` of the analog inputs."""
    driver = instrument.require(CaptureFacet).driver
    analog_names = list(driver.analog_channel_names()) if driver.analog_channel_count else []
    digital, analog = [], []
    for name in names or []:
        if isinstance(name, str) and name in analog_names:
            analog.append((name, analog_names.index(name)))
        else:
            digital.append(name)
    return digital, analog


def channel_numbers(instrument: Instrument, names) -> list[tuple[str, int]]:
    """``(name, channel number)`` of the capture channels ``names`` (pin names, ``CH3``, numbers)."""
    capture = instrument.require(CaptureFacet)
    pins = {pin.name: pin.channel for pin in instrument.pins() if pin.channel is not None}
    driver_names = getattr(getattr(instrument, "simulated_driver", None), "channel_names", None)
    if callable(driver_names):
        pins.update({name: index for index, name in enumerate(driver_names())})
    count = capture.channel_count
    if not names:
        names = list(pins) if pins else [f"CH{index + 1}" for index in range(count)]
    result = []
    for name in names:
        if isinstance(name, int) or (isinstance(name, str) and name.isdigit()):
            number = int(name)
            label = f"CH{number + 1}"
        elif name in pins:
            number, label = pins[name], name
        elif isinstance(name, str) and name.upper().startswith("CH") and name[2:].isdigit():
            number, label = int(name[2:]) - 1, name
        else:
            raise NodeError(f"{instrument.name} has no channel {name!r} (channels: {', '.join(pins) or count})")
        if not 0 <= number < count:
            raise NodeError(f"{instrument.name} has no channel {name!r}")
        result.append((str(label), number))
    return result


def _channel_ports(params: dict):
    channels = params.get("channels") or []
    # The port type follows the device at run time; here only the name is known: A0, AI0 and AIN0 are
    # analog inputs, CH1 is one of a scope (analog) or of a logic analyzer (digital), the rest digital.
    return [], [Out(str(name), _port_type(str(name)), f"channel {name}") for name in channels]


def _port_type(name: str) -> str:
    upper = name.upper()
    if upper.startswith("CH") and upper[2:].isdigit():
        return signals.ANY
    if any(upper.startswith(prefix) and len(upper) > len(prefix) and upper[len(prefix):].isdigit()
           for prefix in ("A", "AI", "AIN")):
        return signals.ANALOG
    return signals.DIGITAL


def block_count(args) -> int:
    """Samples of every channel in the progress of a stream (``CaptureProgressArgs``; also a stream of
    analog channels only)."""
    if args.samples:
        return args.sample_count
    return min((len(values) for values in (args.analog or {}).values()), default=0)


def capture_signal(session: CaptureSession, names: dict[int, str], start: float = 0.0) -> signals.Capture:
    digital = {}
    for channel in session.capture_channels:
        if channel.samples is not None:
            digital[names.get(channel.channel_number, channel.channel_name)] = np.asarray(channel.samples, np.uint8)
    analog, analog_units = {}, {}
    for channel in session.analog_channels:
        if channel.raw is not None:
            analog[channel.display_name] = channel.volts()
            analog_units[channel.display_name] = channel.unit
    return signals.Capture(name="capture", rate=float(session.frequency), start=start, digital=digital,
                           analog=analog, analog_units=analog_units, trigger=session.pre_trigger_samples,
                           session=session)


def limits_of(driver, session: CaptureSession) -> str:
    """What the device allows for the channels of ``session`` (for the message of a refused capture)."""
    from ...core.units import format_quantity

    try:
        mode = session.acquisition_mode
        numbers = session.channel_numbers
        rate = driver.max_frequency_for(numbers, mode)
        limits = driver.get_limits(numbers, mode)
    except Exception:  # noqa: BLE001 - only a better message
        return ""
    total = session.pre_trigger_samples + session.post_trigger_samples
    found = []
    if rate and session.frequency > rate:
        found.append(f"the rate is at most {format_quantity(rate, 'Hz')} (asked: "
                     f"{format_quantity(session.frequency, 'Hz')})")
    if limits.max_total_samples and total > limits.max_total_samples:
        found.append(f"at most {limits.max_total_samples} samples (asked: {total})")
    return (": " + "; ".join(found)) if found else ""


def _trigger(session: CaptureSession, trigger: Any, channels: dict[str, int]) -> None:
    """Fill the trigger of ``session`` from the ``trigger`` parameter."""
    if trigger in (None, "", "none", "immediate", False):
        session.trigger_type = TriggerType.IMMEDIATE
        return
    if not isinstance(trigger, dict):
        raise NodeError("trigger: is none or {edge: rising, source: D0} or {pattern: '10x1', source: D0}")
    source = trigger.get("source")
    number = channels.get(str(source)) if source is not None else next(iter(channels.values()), None)
    if number is None:
        raise NodeError(f"the trigger source {source!r} is none of the captured digital channels "
                        f"({', '.join(channels) or 'none'}): add it to 'channels' (analog inputs do not trigger)")
    if "pattern" in trigger:
        pattern = str(trigger["pattern"]).lower()
        if not pattern or set(pattern) - set("01x"):
            raise NodeError(f"pattern: 0, 1 and x (don't care) from the source on, not {trigger['pattern']!r}")
        session.trigger_type = TriggerType.COMPLEX
        session.trigger_channel = number
        session.trigger_bit_count = len(pattern)
        session.trigger_pattern = sum(1 << index for index, bit in enumerate(pattern) if bit == "1")
        session.trigger_mask = sum(1 << index for index, bit in enumerate(pattern) if bit != "x") \
            if "x" in pattern else None
        return
    edge = str(trigger.get("edge", "rising")).lower()
    if edge not in ("rising", "falling"):
        raise NodeError(f"edge: is rising or falling, not {edge!r}")
    session.trigger_type = TriggerType.EDGE
    session.trigger_channel = number
    session.trigger_inverted = edge == "falling"


@node("device.instrument", title="Device",
      description="An instrument of the flow: its address (sim:uno, pico:/dev/cu.usbmodem1, or the address "
                  "of an instrument open in the device list, which is then used as it is). Without an "
                  "address it is the project's device of the same name. Wire 'device' to the nodes that use it. "
                  "A simulator (sim:uno, sim:pico*2 for a multi device of two boards, sim:uno#2 for a second "
                  "one) simulates what 'signals' says: {scenario: uart, text: Hello, baud: 9600}, "
                  "{scenario: c64}, {scenario: file, path: capture.lac}, or single channels: "
                  "{channels: {D5: {type: square, frequency: 1 kHz}}}.",
      outputs=[Out("device", signals.DEVICE, "the instrument")],
      params=[Param("address", "address", "sim:free"),
              Param("signals", "dict", description="What a simulator simulates (scenario and its settings, "
                                                   "channels: sources of single channels)")], icon="chip")
class InstrumentNode(NodeRuntime):
    """The engine opens the instrument before the flow starts; the node itself does nothing."""


DEVICE_INPUT = In("device", signals.DEVICE, "the instrument (from a device node)", optional=False)


class _DeviceNode(NodeRuntime):
    def instrument(self) -> Instrument:
        return self.ctx.instrument()

    def session(self, instrument: Instrument, stream: bool) -> tuple[CaptureSession, dict[int, str]]:
        digital_names, analog = split_channels(instrument, self.p("channels"))
        channels = channel_numbers(instrument, digital_names) if digital_names or not analog else []
        rate = self.q("rate")
        if not rate or rate <= 0:
            raise NodeError(f"{self.node.id}: rate must be positive")
        samples = self.p("samples")
        if samples is None:
            duration = self.q("duration")
            if duration is None:
                raise NodeError(f"{self.node.id}: give samples or duration")
            samples = int(round(duration * rate))
        samples = int(samples)
        if samples < 1:
            raise NodeError("samples: at least 1")
        if round(rate) < 1:
            raise NodeError("the rate is at least 1 Hz (samples per second)")
        pre = 0 if stream else int(self.p("pre", 0))
        if not 0 <= pre < samples:
            raise NodeError(f"pre: {pre} of the {samples} samples before the trigger - fewer than all of them")
        session = CaptureSession(frequency=int(round(rate)), pre_trigger_samples=pre,
                                 post_trigger_samples=samples - pre)
        session.capture_channels = [AnalyzerChannel(channel_number=number, channel_name=name) for name, number in channels]
        from ...driver.models import AnalogChannel

        session.analog_channels = [AnalogChannel(channel_number=number, channel_name=name) for name, number in analog]
        if stream:
            session.acquisition_mode = ACQUISITION_STREAM
            session.trigger_type = TriggerType.IMMEDIATE
            session.continuous = bool(self.p("until_stopped", False))
        else:
            _trigger(session, self.p("trigger"), {name: number for name, number in channels})
            if self.p("clock"):
                self.clocked(instrument, session, str(self.p("clock")))
        return session, {number: name for name, number in channels}

    def clocked(self, instrument: Instrument, session: CaptureSession, clock: str) -> None:
        """State mode: the samples are taken on the edges of ``clock`` (``rate``: how finely their
        times are stamped)."""
        driver = instrument.require(CaptureFacet).driver
        if not driver.supports_state_mode():
            raise NodeError(f"{instrument.name} has no state mode (samples on the edges of a clock)")
        (_name, number), = channel_numbers(instrument, [clock])
        if number not in driver.state_clock_channels():
            names = [name for name, channel in channel_numbers(instrument, None)
                     if channel in driver.state_clock_channels()]
            raise NodeError(f"{clock} cannot clock the state mode of {instrument.name} (clocks: {', '.join(names)})")
        session.clock_channel = number
        edge = str(self.p("clock_edge", "rising")).lower()
        session.clock_edge = {"rising": EdgeKind.RISING, "falling": EdgeKind.FALLING}.get(edge, EdgeKind.ANY)

    async def wait_for_transfer(self, driver, session: CaptureSession) -> None:
        """A progressively transferred capture: wait until all of it arrived."""
        progressive = session.progressive
        if progressive is None or progressive.complete:
            return
        waiter, complete = self.ctx.external_future()

        def tile(args) -> None:
            if args.session is session and (args.complete or args.error):
                complete(args.error)

        driver.add_capture_tile_handler(tile)
        try:
            if progressive.complete:
                complete(None)
            error = await waiter
        finally:
            driver.remove_capture_tile_handler(tile)
        if error:
            raise NodeError(f"{self.node.id}: {error}")

    # ------------------------------------------------------------------ time
    def local_now(self) -> float:
        """The local clock of acquisitions: ``time.monotonic`` (in virtual time: the flow's)."""
        return self.ctx.now() if self.ctx.fast else time.monotonic()

    def to_flow(self, local: float) -> float:
        return local if self.ctx.fast else local - (time.monotonic() - self.ctx.now())

    def acquisition(self, instrument: Instrument, session: CaptureSession, mode: str, commanded: float) -> Acquisition:
        """The timing of the capture or stream about to start (with the latency measured for it)."""
        rate = float(session.frequency)
        total = session.pre_trigger_samples + session.post_trigger_samples
        model = self.simulator(instrument)
        latency = None if model is not None else stored_latency(calibration_key(instrument, mode, rate, total))
        return timing_of(instrument).begin(Acquisition(rate, commanded, latency, name=instrument.name))

    def command_stamp(self, driver, acquisition: Acquisition) -> None:
        """A device process stamps the start command right before the device got it: a closer lower
        bound than the stamp taken here before the call went through the pipe."""
        stamp = getattr(driver, "command_time", None)
        if stamp is not None and not self.ctx.fast and acquisition.clock.started is not None:
            acquisition.clock.start(max(float(stamp), acquisition.clock.started))

    def simulator(self, instrument: Instrument):
        """The simulator of ``instrument`` when the flow may use its time of the samples: always when it
        runs on the flow's virtual clock, else when it does not emulate USB (then it is found as for a
        real device)."""
        model = getattr(instrument, "simulated_driver", None)
        on_flow_clock = self.ctx.fast and getattr(model, "fast", False)
        return model if model is not None and (on_flow_clock or getattr(model, "knows_time", True)) else None

    def virtual(self, instrument: Instrument, session: CaptureSession):
        """The simulator that captures ``session`` in the flow's virtual time (see
        :meth:`CaptureNode.capture_virtual`); ``None`` in real time and for other devices."""
        model = getattr(instrument, "simulated_driver", None) if self.ctx.fast else None
        if model is None or not getattr(model, "fast", False) or not hasattr(model, "take") \
                or session.clock_channel is not None:
            return None  # (a simulator of the device list runs in real time, on its own clock)
        return model

    def simulator_to_local(self, model, at: float) -> float:
        """A time of the simulator's clock on the local clock of acquisitions."""
        return at - (model.clock() - self.local_now())

    @staticmethod
    def device_stamp(acquisition: Acquisition, session: CaptureSession) -> None:
        """A device that stamped its first sample in a time scale (``device_start_time`` and
        ``device_timescale`` of the session, set by its driver)."""
        start: Optional[float] = getattr(session, "device_start_time", None)
        timescale = getattr(session, "device_timescale", None)
        if start is not None and timescale:
            acquisition.stamped(float(start), str(timescale), float(getattr(session, "device_time_accuracy", 0.0)))

    async def wait_device_time(self, instrument: Instrument, start: float) -> float:
        """In virtual time: wait until the device finished the capture; returns its trigger time."""
        model = getattr(instrument, "simulated_driver", None)
        if model is None:
            return self.ctx.now()
        end = model.last_capture_end
        if self.ctx.fast and end > self.ctx.now():
            await self.ctx.sleep(end - self.ctx.now())
        return model.last_trigger_time


@node("device.capture", title="Capture",
      description="Captures with an instrument: channels, rate, length and trigger. Without a wire at "
                  "'arm' it captures once when the flow starts, otherwise on every value at 'arm'. With a "
                  "'clock' channel it takes one sample on each edge of the clock instead (state mode, "
                  "'samples' states; 'rate' stamps their times) and sends them at 'states'.",
      inputs=[DEVICE_INPUT, In("arm", signals.ANY, "start a capture")],
      outputs=[Out("capture", signals.CAPTURE), Out("states", signals.STATES, "state mode: the values on the "
                                                                                 "clock's edges"),
               Out("done", signals.EVENT),
               Out("armed", signals.EVENT, "the capture waits for its trigger (start a stimulus)")],
      params=[Param("channels", "list", description="names, e.g. [D0, D1]", suggest="channels"),
              Param("rate", "quantity", "1 MHz", "Hz"), Param("samples", "int"), Param("duration", "quantity", unit="s"),
              Param("pre", "int", 0), Param("trigger", "dict", description="{edge: rising, source: D0}"),
              Param("clock", "str", description="state mode: the channel whose edges take the samples",
                    suggest="channels"),
              Param("clock_edge", "choice", "rising", choices=("rising", "falling", "both"))],
      ports=_channel_ports, icon="record")
class CaptureNode(_DeviceNode):
    async def run(self) -> None:
        if not self.ctx.wired("arm"):
            await self.capture()

    async def on_input(self, port: str, value: Any) -> None:
        if port == "arm":
            await self.capture()

    async def capture(self) -> None:
        instrument = self.instrument()
        driver = instrument.require(CaptureFacet).driver
        session, names = self.session(instrument, stream=False)
        model = self.virtual(instrument, session)
        if model is not None:
            await self.capture_virtual(instrument, model, session, names)
            return
        waiter, finished = self.ctx.external_future()
        arrival = self.ctx.now if self.ctx.fast else time.monotonic
        arrived: list[float] = []

        def complete(result) -> None:
            # when the capture came: stamped where the driver had it (else now)
            stamp = getattr(result, "arrived", None) if not self.ctx.fast else None
            arrived.append(stamp if stamp is not None else arrival())
            finished(result)

        start = self.ctx.now()
        commanded = self.local_now()
        acquisition = self.acquisition(instrument, session, "buffer", commanded)
        if session.trigger_type != TriggerType.IMMEDIATE:
            acquisition.clock.started = None  # it waited for its trigger: the command bounds nothing
        # (a real device answers the start in a thread: it may take seconds or not answer)
        error = await self.ctx.device_call(instrument, driver.start_capture, session, complete)
        if error != CaptureError.NONE:
            complete(None)
            await waiter  # keeps the engine's bookkeeping of external waits balanced
            raise NodeError(f"the capture could not be started ({error.message}){limits_of(driver, session)}")
        self._running = driver  # stopped by cleanup() when the flow ends before the capture does
        self.command_stamp(driver, acquisition)
        self.ctx.emit("armed", signals.Event(times=[self.ctx.now()], data=[None]))
        result = await waiter
        self._running = None
        if result is None or not result.success:
            raise NodeError(f"{self.node.id}: the capture failed" + (f": {result.error}" if result and result.error else ""))
        await self.wait_for_transfer(driver, result.session)
        trigger_time = await self.wait_device_time(instrument, start)
        model = self.simulator(instrument)
        if model is not None:
            acquisition.exact_start = self.simulator_to_local(model, trigger_time - session.pre_trigger_samples
                                                              / session.frequency)
        else:
            total = result.session.pre_trigger_samples + result.session.post_trigger_samples
            acquisition.arrived(max(total - 1, 0), arrived[0] if arrived else self.local_now())
            self.device_stamp(acquisition, result.session)
        local_start = acquisition.time_of(0)
        acquisition.given(local_start, 0)
        if result.session.clock_channel is not None:
            self.emit_states(result.session, names, self.to_flow(local_start))
            return
        capture = capture_signal(result.session, names, start=self.to_flow(local_start))
        self.ctx.emit("capture", capture)
        for name in capture.channels:
            if name in self.ctx.output_ports:
                self.ctx.emit(name, capture.channel(name))
        self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[capture.sample_count]))

    def emit_states(self, session: CaptureSession, names: dict, start: float) -> None:
        """A state capture: the values on the clock's edges (bit 0: the first channel) at their times."""
        channels = [names.get(channel.channel_number, channel.channel_name) for channel in session.capture_channels]
        values = np.zeros(session.post_trigger_samples, dtype=np.uint32)
        for bit, channel in enumerate(session.capture_channels):
            values |= np.asarray(channel.samples[:len(values)], dtype=np.uint32) << np.uint32(bit)
        times = None if session.state_times is None else start + np.asarray(session.state_times, np.float64) / 1e6
        states = signals.States(name="states", values=values, times=times, channels=channels)
        self.ctx.emit("states", states)
        for bit, name in enumerate(channels):
            if name in self.ctx.output_ports:
                self.ctx.emit(name, states.bit(bit))
        self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[len(values)]))

    async def capture_virtual(self, instrument: Instrument, model, session: CaptureSession, names: dict) -> None:
        """Virtual time: the flow's time runs on while the capture waits for its trigger and records;
        the samples are taken after its end, with what the flow did meanwhile (a stimulus after
        'armed', after a wait)."""
        error = CaptureError.BUSY if model.is_capturing else model.check_capture(session)
        if error != CaptureError.NONE:
            raise NodeError(f"the capture could not be started ({error.message}){limits_of(model, session)}")
        from ...driver.simulated.device import TRIGGER_SEARCH_LIMIT

        rate = float(session.frequency)
        pre, post = session.pre_trigger_samples, session.post_trigger_samples
        acquisition = self.acquisition(instrument, session, "buffer", self.local_now())
        searched = model.virtual_start(session) + pre / rate
        try:
            deadline = searched + TRIGGER_SEARCH_LIMIT
            self.ctx.emit("armed", signals.Event(times=[self.ctx.now()], data=[None]))
            trigger = searched if session.trigger_type in (TriggerType.IMMEDIATE, TriggerType.SIMULATION) else None
            # in steps of the capture's length (1 ms to 100 ms): the signal up to the flow's time is known
            step = min(max((pre + post) / rate, 0.001), 0.1)
            while trigger is None:
                until = min(max(self.ctx.now(), searched) + step, deadline)
                await self.ctx.sleep_until(until)
                trigger = model.find_trigger(session, searched, until)
                if trigger is None:
                    if until >= deadline:
                        raise NodeError(f"the capture failed: No trigger within {TRIGGER_SEARCH_LIMIT:g} s of signal.")
                    searched = until
            await self.ctx.sleep_until(trigger + post / rate)
            first = trigger - pre / rate
            model.take(session, first)
        finally:
            model.virtual_end()
        acquisition.exact_start = self.simulator_to_local(model, first)
        acquisition.given(acquisition.time_of(0), 0)
        capture = capture_signal(session, names, start=self.to_flow(acquisition.time_of(0)))
        self.ctx.emit("capture", capture)
        for name in capture.channels:
            if name in self.ctx.output_ports:
                self.ctx.emit(name, capture.channel(name))
        self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[capture.sample_count]))

    def cleanup(self) -> None:
        driver, self._running = getattr(self, "_running", None), None
        if driver is not None and driver.is_capturing:
            driver.stop_capture()  # e.g. still waiting for its trigger when the flow was stopped


@node("device.stream", title="Stream",
      description="Streams the channels of an instrument; blocks of samples flow while it runs. "
                  "Starts with the flow (or on 'start'), ends after its duration or on 'stop'.",
      inputs=[DEVICE_INPUT, In("start", signals.ANY), In("stop", signals.ANY)],
      outputs=[Out("capture", signals.CAPTURE, "the blocks"), Out("done", signals.EVENT)],
      params=[Param("channels", "list", suggest="channels"),
              Param("rate", "quantity", "100 kHz", "Hz"), Param("samples", "int"), Param("duration", "quantity", unit="s"),
              Param("until_stopped", "bool", False,
                    description="stream until 'stop' (or the flow's end); 'samples'/'duration' is then what is kept")],
      ports=_channel_ports, icon="wave")
class StreamNode(_DeviceNode):
    pulled = ("start",)

    async def setup(self) -> None:
        self._driver = None
        #: the flow time of a 'stop' (virtual time: the blocks after it are not sent)
        self._stop_at: Optional[float] = None

    async def run(self) -> None:
        if self.ctx.wired("start"):
            await self.ctx.receive("start")
        instrument = self.instrument()
        driver = instrument.require(CaptureFacet).driver
        self._driver = driver
        session, names = self.session(instrument, stream=True)
        model = self.virtual(instrument, session)
        if model is not None:
            self._driver = None  # (a 'stop' ends it in the flow's time, not on the device)
            await self.stream_virtual(instrument, model, session, names)
            return
        queue = self.ctx.external_queue()
        emitted = 0
        start = self.ctx.now()
        rate = float(session.frequency)
        model = self.simulator(instrument)
        acquisition = self.acquisition(instrument, session, "stream", self.local_now())
        arrival = self.ctx.now if self.ctx.fast else time.monotonic

        def progress(args) -> None:
            if args.session is session:
                if model is None:  # when the block came: stamped where the driver read it
                    stamp = args.arrived if args.arrived is not None and not self.ctx.fast else arrival()
                    acquisition.arrived(args.first_sample + block_count(args) - 1, stamp)
                queue.put(("block", args))

        def completed(args) -> None:
            queue.put(("done", args))
            queue.close()

        driver.add_capture_progress_handler(progress)
        try:
            error = await self.ctx.device_call(instrument, driver.start_capture, session, completed)
            if error != CaptureError.NONE:
                queue.close()
                raise NodeError(f"the stream could not be started ({error.message}){limits_of(driver, session)}")
            if model is not None:
                acquisition.exact_start = self.simulator_to_local(model, model.last_stream_start)
            else:
                self.command_stamp(driver, acquisition)
                self.device_stamp(acquisition, session)
            result = None
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, args = item
                if kind == "done":
                    result = args
                    continue
                first = args.first_sample
                count = block_count(args)
                if first + count <= emitted:
                    continue
                offset = max(emitted - first, 0)
                block = {names[number]: np.array(values[offset:count]) for number, values in args.samples.items()}
                # analog channels: their raw counts in volts (the driver set scale and offset)
                analog = {channel.display_name: channel.scale * np.asarray(args.analog[channel.channel_number]
                                                                           [offset:count], dtype=np.float64)
                          + channel.offset for channel in session.analog_channels
                          if channel.channel_number in (args.analog or {})}
                units = {channel.display_name: channel.unit for channel in session.analog_channels}
                local_start = acquisition.time_of(first + offset)
                acquisition.given(local_start, first + offset)
                block_start = self.to_flow(local_start)
                emitted = first + count
                self.emit_block(session, names, rate, block_start, block, analog, units)
                await self.ctx.engine._drain(self.node.id)
            self._ended = True
            if result is None or not result.success:
                raise NodeError(f"{self.node.id}: the stream failed" + (f": {result.error}" if result and result.error else ""))
            await self.wait_device_time(instrument, start)
            self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[emitted]))
        finally:
            driver.remove_capture_progress_handler(progress)
            queue.close()

    async def stream_virtual(self, instrument: Instrument, model, session: CaptureSession, names: dict) -> None:
        """Virtual time: each block is taken when the flow's time reached its end, so what the flow
        does meanwhile is in it, and a 'stop' ends the stream where it came."""
        error = CaptureError.BUSY if model.is_capturing else model.check_capture(session)
        if error != CaptureError.NONE:
            raise NodeError(f"the stream could not be started ({error.message}){limits_of(model, session)}")
        start = model.virtual_start(session)
        try:
            await self.blocks_virtual(instrument, model, session, names, start)
        finally:
            model.virtual_end()

    async def blocks_virtual(self, instrument: Instrument, model, session: CaptureSession, names: dict,
                             start: float) -> None:
        from ...driver.simulated.device import STREAM_BLOCK

        rate = float(session.frequency)
        wanted = session.post_trigger_samples
        # an endless stream without a 'stop' ends after four times its length (virtual time never ends)
        limit = wanted * (4 if not self.ctx.wired("stop") else 1000) if session.continuous else wanted
        overflow = model.take_overflow()
        model.last_stream_start = start
        acquisition = self.acquisition(instrument, session, "stream", self.local_now())
        acquisition.exact_start = self.simulator_to_local(model, start)
        block = max(int(rate * STREAM_BLOCK), 1)
        units = {channel.display_name: channel.unit for channel in session.analog_channels}
        emitted = 0
        while emitted < limit:
            count = min(block, limit - emitted)
            block_start = start + emitted / rate
            await self.ctx.sleep_until(block_start + count / rate)
            if self._stop_at is not None:
                count = min(count, max(int(round((self._stop_at - block_start) * rate)), 0))
                if not count:
                    break
            digital, raw = model.take_block(session, block_start, count)
            local_start = acquisition.time_of(emitted)
            acquisition.given(local_start, emitted)
            self.emit_block(session, names, rate, self.to_flow(local_start),
                            {names[number]: values for number, values in digital.items()},
                            {channel.display_name: channel.scale * raw[channel.channel_number].astype(np.float64)
                             + channel.offset for channel in session.analog_channels}, units)
            emitted += count
            if overflow and emitted >= max(wanted // 3, 1):
                raise NodeError("the stream failed: The device could not keep up with the stream, so it stopped "
                                "early (overflow).")
            if self._stop_at is not None:
                break
            await self.ctx.engine._drain(self.node.id)
        self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[emitted]))

    def emit_block(self, session: CaptureSession, names: dict, rate: float, start: float, block: dict,
                   analog: dict, units: dict) -> None:
        self.ctx.emit("capture", signals.Capture(name="stream", rate=rate, start=start, digital=block, analog=analog,
                                                 analog_units={name: units[name] for name in analog}))
        timebase = signals.TimeBase.uniform(rate, start)
        for name, values in block.items():
            if name in self.ctx.output_ports:
                self.ctx.emit(name, signals.Digital(name=name, values=values, time=timebase))
        for name, values in analog.items():
            if name in self.ctx.output_ports:
                self.ctx.emit(name, signals.Analog(name=name, unit=units[name], values=values, time=timebase))

    def cleanup(self) -> None:
        driver = getattr(self, "_driver", None)
        if driver is not None and not getattr(self, "_ended", False) and driver.is_capturing:
            driver.stop_capture()  # an endless stream does not outlive its flow

    async def on_input(self, port: str, value: Any) -> None:
        if port == "stop":
            self._stop_at = self.ctx.now()
            if self._driver is not None and self._driver.is_capturing:
                self._driver.stop_capture()


NODES = collect(globals())
