# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Time nodes: a sync signal, aligning instruments with it, measuring their latency.

``timing.sync`` drives a pin with edges at irregular intervals: every instrument that records it
(and a remote device whose input records it, ``remote.sync``) can be matched with the others
without ambiguity. ``timing.align`` aligns an instrument with a reference instrument by that signal:
offset and drift (only the offset when their sample clocks are shared), applied to the
instrument's later samples and to what is wired through it. ``timing.calibrate`` measures how late
the samples of an instrument arrive, with a loopback from one of its outputs to one of its inputs;
the latency is kept for the instrument and its settings and used by its captures and streams from
then on (``docs/timing.md``).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from typing import Any, Optional

import numpy as np

from ...core import signals
from ...core.instrument import MODE_OUTPUT, CaptureFacet, GpioFacet, InstrumentError
from ...core.timing import (
    Acquisition,
    Correction,
    calibration_key,
    edges,
    fit_correction,
    match,
    store_latency,
    timing_of,
)
from ...core.timing_tools import LatencyError, loopback_latency, loopback_plan, shares_clock
from ...driver.base import ACQUISITION_STREAM, CaptureError
from ...driver.models import AnalyzerChannel, CaptureSession, TriggerType
from ..engine.runtime import NodeError, NodeRuntime
from .device import DEVICE_INPUT, channel_numbers
from .registry import In, Out, Param, collect, node

#: edges kept for the fit of an alignment
WINDOW = 64


def shifted(value: Any, seconds: float) -> Any:
    """``value`` (a signal) ``seconds`` later."""
    if not seconds:
        return value
    if isinstance(value, signals.Capture):
        return replace(value, start=value.start + seconds)
    if isinstance(value, (signals.Digital, signals.Analog)):
        return replace(value, time=value.time.shifted(seconds))
    if isinstance(value, signals.Event):
        return replace(value, times=np.asarray(value.times, dtype=np.float64) + seconds)
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return replace(value, at=value.at + seconds)
    return value


class _TimingNode(NodeRuntime):
    def local(self, flow: float) -> float:
        """A flow time on the local clock of acquisitions (``time.monotonic``; virtual: the flow's)."""
        return flow if self.ctx.fast else flow + (time.monotonic() - self.ctx.now())

    def flow(self, local: float) -> float:
        return local if self.ctx.fast else local - (time.monotonic() - self.ctx.now())

    async def act(self, action) -> Any:
        try:
            return await self.ctx.device_call(self.ctx.instrument(), action)
        except InstrumentError as error:
            raise NodeError(f"{self.node.id}: {error}") from None


@node("timing.sync", title="Sync signal",
      description="Drives a pin with a sync signal: edges at irregular intervals (the same for the same 'seed') "
                  "that recordings of it can be matched by without ambiguity. Wire the pin to a channel of every "
                  "instrument to align (timing.align) and to the sync input of a remote device (remote.sync). "
                  "'edges' are the times the edges were commanded (as precise as the output). Runs for "
                  "'duration' (0: until 'stop' or the end of the flow).",
      inputs=[DEVICE_INPUT, In("stop", signals.ANY)],
      outputs=[Out("edges", signals.EVENT, "the time of every edge and its level")],
      params=[Param("pin", "str", required=True, suggest="pins:DOUT"),
              Param("duration", "quantity", "10 s", "s"),
              Param("min_interval", "quantity", "20 ms", "s"), Param("max_interval", "quantity", "60 ms", "s"),
              Param("seed", "int", 1)],
      icon="clock")
class SyncNode(_TimingNode):
    async def setup(self) -> None:
        self.stopped = False

    async def run(self) -> None:
        from openscilab_device.timing import sync_intervals

        instrument = self.ctx.instrument()
        gpio = instrument.facet(GpioFacet)
        if gpio is None:
            raise NodeError(f"{self.node.id}: {instrument.name} has no GPIO")
        pin = str(self.p("pin") or "")
        if not pin:
            raise NodeError(f"{self.node.id}: no pin")
        low, high = self.q("min_interval") or 0.02, self.q("max_interval") or 0.06
        if not 0 < low <= high:
            raise NodeError(f"{self.node.id}: 0 < min_interval <= max_interval")
        intervals = sync_intervals(int(self.p("seed", 1)), low, high)
        duration = self.q("duration") or 0.0
        if gpio.mode(pin) != MODE_OUTPUT:
            await self.act(lambda: gpio.set_mode(pin, MODE_OUTPUT))
        await self.act(lambda: gpio.write(pin, 0))
        level, begin = 0, self.ctx.now()
        due = begin + next(intervals)
        while not self.stopped and (duration <= 0 or due < begin + duration):
            await self.ctx.sleep_until(due)
            if self.stopped:
                break
            level ^= 1
            at = self.ctx.now()
            await self.act(lambda level=level: gpio.write(pin, level))
            self.ctx.emit("edges", signals.Event(name=pin, times=[at], data=[level]))
            due += next(intervals)

    async def on_input(self, port: str, value: Any) -> None:
        if port == "stop":
            self.stopped = True


@node("timing.align", title="Align",
      description="Aligns an instrument (at 'device') with a reference by a sync signal both recorded: 'signal' "
                  "is the signal as this instrument recorded it, 'reference' as the reference instrument did "
                  "(or 'edges' of timing.sync: as precise as its output). The matched edges give offset and "
                  "drift (drift none: their sample clocks are shared, only the offset); the instrument's later "
                  "samples are placed with them, and what arrives at 'in' leaves 'out' moved onto the "
                  "reference's time (a capture of the instrument, its other channels).",
      inputs=[DEVICE_INPUT, In("signal", signals.DIGITAL, "the sync signal recorded by this instrument"),
              In("reference", signals.DIGITAL, "the sync signal recorded by the reference"),
              In("edges", signals.EVENT, "edge times of the sync signal (timing.sync)"),
              In("in", signals.ANY, "signals of this instrument to move")],
      outputs=[Out("out", signals.ANY, "'in' on the reference's time"), Out("offset", signals.SCALAR),
               Out("uncertainty", signals.SCALAR), Out("edges", signals.SCALAR)],
      params=[Param("drift", "choice", "auto", choices=("auto", "fit", "none"),
                    description="auto: as the device card says (Timing: sample clock); fit: offset and drift; "
                                "none: shared sample clock, offset only"),
              Param("search", "quantity", "1 s", "s", description="how far apart the recordings may be")],
      icon="align")
class AlignNode(_TimingNode):
    async def setup(self) -> None:
        self.recorded: list[tuple[Optional[float], float]] = []   # (sample index, local time as given)
        self.reference: list[float] = []
        self.correction: Optional[Correction] = None
        self.acquisition: Optional[Acquisition] = None
        self.pending: list = []

    async def run(self) -> None:
        if not self.ctx.wired("signal") or not (self.ctx.wired("reference") or self.ctx.wired("edges")):
            raise NodeError(f"{self.node.id}: wire 'signal' and 'reference' (or 'edges')")

    def index(self, local: float) -> Optional[float]:
        """The sample index of an edge of this instrument (``None``: not from its acquisition)."""
        acquisition = timing_of(self.ctx.instrument()).current
        if acquisition is None:
            return None
        self.acquisition = acquisition
        return acquisition.index_of(local)

    def raw(self, index: Optional[float], local: float) -> float:
        """The time of an edge before any correction - with what the acquisition knows *now*, so all
        edges are placed alike (its estimate gets better while blocks arrive)."""
        if index is None or self.acquisition is None:
            return local
        return self.acquisition.raw(index).time

    async def on_input(self, port: str, value: Any) -> None:
        if port == "in":
            self.forward(value)
            return
        if port == "signal" and isinstance(value, signals.Digital) and value.time.is_uniform and len(value) > 1:
            times = edges(value.values, value.time.start, value.time.rate)[0]
            self.recorded = (self.recorded + [(self.index(self.local(t)), self.local(t)) for t in times])[-4 * WINDOW:]
        elif port == "reference" and isinstance(value, signals.Digital) and value.time.is_uniform and len(value) > 1:
            times = edges(value.values, value.time.start, value.time.rate)[0]
            self.reference = (self.reference + [self.local(t) for t in times])[-4 * WINDOW:]
        elif port == "edges" and isinstance(value, signals.Event):
            self.reference = (self.reference + [self.local(float(t)) for t in value.times])[-4 * WINDOW:]
        else:
            return
        self.fit()

    def fit(self) -> None:
        if len(self.recorded) < 4 or len(self.reference) < 4:
            return
        raw = np.array([self.raw(index, local) for index, local in self.recorded])
        pairs = match(np.array(self.reference), raw, self.q("search") or 1.0)
        if len(pairs) < 4:
            return
        pairs = pairs[-WINDOW:]
        reference = self.reference_instrument()
        state = timing_of(reference).state() if reference is not None else None
        base = state.uncertainty if state is not None and math.isfinite(state.uncertainty) else 0.0
        drift = str(self.p("drift", "auto"))
        if drift == "auto":
            drift = "none" if shares_clock(self.ctx.instrument()) else "fit"
        correction = fit_correction(pairs, fit_drift=drift == "fit",
                                    reference=reference.name if reference is not None else "timing.sync",
                                    base_uncertainty=base)
        self.correction = correction
        if self.acquisition is not None:
            self.acquisition.correct(correction)
        at = self.ctx.now()
        self.ctx.emit("offset", signals.Scalar(name="offset", unit="s", value=correction.offset, at=at))
        self.ctx.emit("uncertainty", signals.Scalar(name="uncertainty", unit="s", value=correction.uncertainty, at=at))
        self.ctx.emit("edges", signals.Scalar(name="edges", value=float(correction.edges), at=at))
        pending, self.pending = self.pending, []
        for value in pending:
            self.forward(value)

    def reference_instrument(self):
        """The instrument that recorded 'reference' (the device of the node wired to it)."""
        flow = self.ctx.engine.flow
        for edge in flow.edges_into(self.node.id, "reference"):
            name = flow.device_of(edge.source.node, "device")
            if name is not None:
                return self.ctx.engine.device(name)
        return None

    def forward(self, value: Any) -> None:
        """``value`` moved by the correction (it waits for the first one)."""
        correction = self.correction
        if correction is None:
            self.pending.append(value)
            return
        start = _start_of(value)
        if start is None:
            self.ctx.emit("out", value)
            return
        local = self.local(start)
        moved = correction.apply(self.raw(self.index(local), local))
        self.ctx.emit("out", shifted(value, moved - local))


def _start_of(value: Any) -> Optional[float]:
    if isinstance(value, signals.Capture):
        return value.start
    if isinstance(value, (signals.Digital, signals.Analog)) and value.time.is_known:
        return value.time.time_of(0)
    return None


@node("timing.calibrate", title="Calibrate latency",
      description="Measures how late the samples of an instrument arrive: an output pin, wired to one of its "
                  "channels, is switched 'repeats' times while the instrument streams (or captures) that channel. "
                  "No edge is earlier than its command and no sample later than its arrival: half of the shortest "
                  "loop is the latency. It is kept for the instrument at this rate (and length of a capture) and "
                  "used by its captures and streams from then on. Real time only.",
      inputs=[DEVICE_INPUT], outputs=[Out("latency", signals.SCALAR), Out("uncertainty", signals.SCALAR)],
      params=[Param("pin", "str", required=True, suggest="pins:DOUT"),
              Param("channel", "str", required=True, suggest="channels"),
              Param("rate", "quantity", "100 kHz", "Hz"),
              Param("mode", "choice", "stream", choices=("stream", "capture")),
              Param("samples", "int", 100_000, description="length of a capture (mode capture)"),
              Param("repeats", "int", 10), Param("interval", "quantity", "30 ms", "s"),
              Param("store", "bool", True, description="keep the latency for the instrument")],
      icon="clock")
class CalibrateNode(_TimingNode):
    async def run(self) -> None:
        if self.ctx.fast:
            raise NodeError(f"{self.node.id}: a latency is measured in real time (switch 'Fast' off)")
        instrument = self.ctx.instrument()
        gpio = instrument.facet(GpioFacet)
        capture = instrument.facet(CaptureFacet)
        if gpio is None or capture is None:
            raise NodeError(f"{self.node.id}: {instrument.name} needs GPIO and capture channels")
        pin = str(self.p("pin") or "")
        (_label, number), = channel_numbers(instrument, [self.p("channel")])
        rate = self.q("rate") or 100_000.0
        repeats, interval = int(self.p("repeats", 10)), self.q("interval") or 0.03
        mode = str(self.p("mode", "stream"))
        driver = capture.driver
        try:
            total, repeats, interval = loopback_plan(instrument, str(self.p("channel")), rate, mode,
                                                     int(self.p("samples", 100_000)), repeats, interval)
        except LatencyError as error:
            raise NodeError(f"{self.node.id}: {error}") from None
        session = CaptureSession(frequency=int(round(rate)), pre_trigger_samples=0, post_trigger_samples=total,
                                 trigger_type=TriggerType.IMMEDIATE)
        session.capture_channels = [AnalyzerChannel(channel_number=number)]
        if mode == "stream":
            session.acquisition_mode = ACQUISITION_STREAM
        acquisition = Acquisition(rate, time.monotonic())
        commands: list[float] = []
        queue = self.ctx.external_queue()

        def progress(args) -> None:
            if args.session is session:
                acquisition.arrived(args.first_sample + args.sample_count - 1, args.arrived or time.monotonic())
                queue.put(("block", args))

        def completed(args) -> None:
            if mode == "capture":
                acquisition.arrived(total - 1, getattr(args, "arrived", None) or time.monotonic())
            queue.put(("done", args))
            queue.close()

        if gpio.mode(pin) != MODE_OUTPUT:
            await self.act(lambda: gpio.set_mode(pin, MODE_OUTPUT))
        await self.act(lambda: gpio.write(pin, 0))
        await self.ctx.sleep(interval)

        def switch(level: int) -> None:
            commands.append(time.monotonic())  # (right before the command, in the device's thread)
            gpio.write(pin, level)

        driver.add_capture_progress_handler(progress)
        try:
            error = await self.ctx.device_call(instrument, driver.start_capture, session, completed)
            if error != CaptureError.NONE:
                queue.close()
                raise NodeError(f"{self.node.id}: the {mode} could not be started ({error.message})")
            stamp = getattr(driver, "command_time", None)  # (a device process stamps the start itself)
            if stamp is not None and acquisition.clock.started is not None:
                acquisition.clock.start(max(float(stamp), acquisition.clock.started))
            toggles = asyncio.ensure_future(self.toggle(switch, repeats, interval))
            samples = None
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, args = item
                if kind == "done":
                    if not args.success:
                        raise NodeError(f"{self.node.id}: the {mode} failed: {args.error}")
                    samples = args.session.capture_channels[0].samples
            await toggles
        finally:
            driver.remove_capture_progress_handler(progress)
            queue.close()
        if samples is None:
            raise NodeError(f"{self.node.id}: no samples")
        try:
            latency, _count = loopback_latency(samples, rate, commands, acquisition, repeats)
        except LatencyError as error:
            raise NodeError(f"{self.node.id}: {error} on {self.p('channel')} - is {pin} wired to it?") from None
        if self.p("store", True):
            store_latency(calibration_key(instrument, "stream" if mode == "stream" else "buffer", rate, total), latency)
        self.ctx.emit("latency", signals.Scalar(name="latency", unit="s", value=latency.value, at=self.ctx.now()))
        self.ctx.emit("uncertainty", signals.Scalar(name="uncertainty", unit="s", value=latency.uncertainty,
                                                    at=self.ctx.now()))

    async def toggle(self, switch, repeats: int, interval: float) -> None:
        await self.ctx.sleep(interval)
        for index in range(repeats):
            await self.act(lambda level=(index + 1) % 2: switch(level))
            await self.ctx.sleep(interval)


NODES = collect(globals())
