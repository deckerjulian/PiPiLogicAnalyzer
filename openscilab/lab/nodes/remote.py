# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Nodes of remote devices (scripts on other computers, ``openscilab_device``).

Every value of a remote device carries the time it was measured on the device; the connection
converts it into local time with the measured clock of the device (or a sync signal, see
``remote.sync``), so values of several devices line up however long the network took.
``remote.receive`` hands them on in the order of that time: it waits a moment for late values
(``playout``, by default what the network needs), so values of two devices arrive in the right
order. ``remote.set`` sends a value with the time it should take effect, a little ahead: the
device applies it at that moment, not when the message arrives.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Optional

import numpy as np

from ...core import signals, timing, units
from ...driver.remote.instrument import RemoteFacet
from ..engine.runtime import NodeError, NodeRuntime
from .device import DEVICE_INPUT
from .registry import In, Out, Param, node

_TYPES = {"analog": signals.ANALOG, "digital": signals.DIGITAL, "scalar": signals.SCALAR, "bool": signals.BOOL,
          "event": signals.EVENT, "text": signals.EVENT}


def _seconds(value: Any, automatic: float) -> float:
    """A parameter in seconds; ``"auto"`` (or empty): ``automatic``."""
    if value in (None, "", "auto"):
        return automatic
    try:
        return float(units.parse(value, "s"))
    except (ValueError, units.UnitError) as error:
        raise NodeError(f"{value!r}: {error} (or 'auto')") from None


class _RemoteNode(NodeRuntime):
    def remote(self) -> RemoteFacet:
        if self.ctx.fast:
            raise NodeError(f"{self.node.id}: a remote device runs in real time")
        instrument = self.ctx.instrument()
        facet = instrument.facet(RemoteFacet)
        if facet is None:
            raise NodeError(f"{self.node.id}: {instrument.name} is no remote device")
        return facet

    def flow_time(self, local: float) -> float:
        """A local time (``time.monotonic``) as flow time."""
        return local - (time.monotonic() - self.ctx.now())

    def local_time(self, flow: float) -> float:
        return flow + (time.monotonic() - self.ctx.now())


@node("remote.receive", title="Receive",
      description="The values of an input of a remote device, with the time they were measured on the "
                  "device (converted with its measured clock): numbers and truth values one by one, samples "
                  "as blocks of an analog or digital signal. 'playout' waits that long for late values so "
                  "the values of several devices come in the order of their time (auto: what the network "
                  "needs; 0: at once). Ends after 'duration' (0: with the flow).",
      inputs=[DEVICE_INPUT], outputs=[Out("out", signals.ANY, "every value or block")],
      params=[Param("channel", "str", required=True, suggest="remote_inputs"),
              Param("playout", "str", "auto", description="auto, 0 or a time (e.g. 20 ms)"),
              Param("duration", "quantity", 0, "s")],
      icon="import")
class ReceiveNode(_RemoteNode):
    async def run(self) -> None:
        remote = self.remote()
        channel = str(self.p("channel") or "")
        spec = remote.inputs().get(channel)
        if spec is None:
            raise NodeError(f"{self.node.id}: {remote.device_name} has no input {channel!r} "
                            f"(inputs: {', '.join(remote.inputs()) or 'none'})")
        playout = _seconds(self.p("playout", "auto"), 2 * remote.typical_delay() + 0.005)
        queue = self.ctx.external_queue()
        stop = remote.listen(channel, queue.put)
        duration = self.q("duration") or 0.0
        timer = threading.Timer(duration, queue.close) if duration > 0 else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                at = self.flow_time(item.time)
                if playout > 0:
                    end = at + (len(item.samples) / item.rate if item.samples is not None else 0.0)
                    await self.ctx.sleep_until(end + playout)
                self.ctx.emit("out", self.value(item, spec, at))
        finally:
            stop()
            if timer is not None:
                timer.cancel()
            queue.close()

    @staticmethod
    def value(item, spec: dict, at: float):
        name, unit, kind = item.channel, str(spec.get("unit") or ""), spec["kind"]
        if kind == "analog":
            return signals.Analog(name=name, unit=unit or "V", values=item.samples,
                                  time=signals.TimeBase.uniform(item.rate, at))
        if kind == "digital":
            return signals.Digital(name=name, values=item.samples, time=signals.TimeBase.uniform(item.rate, at))
        if kind == "scalar":
            return signals.Scalar(name=name, unit=unit, value=scalar_value(item.value, unit), at=at)
        if kind == "bool":
            return signals.Bool(name=name, value=bool(item.value), at=at)
        return signals.Event(name=name, times=[at], data=[item.value])


@node("remote.set", title="Set",
      description="Sets an output of a remote device to the value arriving at 'value' (or to the parameter "
                  "'value' once at the start). The value is sent 'lead' ahead with the time it should take "
                  "effect, and the device applies it at that time (auto: what the network needs, 0: as soon "
                  "as it arrives). 'done' tells the time it happened.",
      inputs=[DEVICE_INPUT, In("value", signals.ANY)],
      outputs=[Out("done", signals.EVENT, "the time the device applied the value")],
      params=[Param("channel", "str", required=True, suggest="remote_outputs"), Param("value", "any"),
              Param("lead", "str", "auto", description="auto, 0 or a time (e.g. 50 ms)")],
      icon="export")
class SetNode(_RemoteNode):
    async def run(self) -> None:
        self.remote()  # (fails early: real time, a remote device)
        if not self.ctx.wired("value") and self.p("value") is not None:
            await self.apply(self.p("value"))

    async def on_input(self, port: str, value: Any) -> None:
        await self.apply(value)

    async def apply(self, value: Any) -> None:
        remote = self.remote()
        channel = str(self.p("channel") or "")
        spec = remote.outputs().get(channel)
        if spec is None:
            raise NodeError(f"{self.node.id}: {remote.device_name} has no output {channel!r} "
                            f"(outputs: {', '.join(remote.outputs()) or 'none'})")
        value = _plain(value)
        try:
            if spec["kind"] == "bool":
                value = _truth(value)
            elif spec["kind"] == "scalar":
                value = float(value)
        except (TypeError, ValueError):
            raise NodeError(f"{channel} takes a {spec['kind']}, not {value!r}") from None
        lead = _seconds(self.p("lead", "auto"), 2 * remote.typical_delay() + 0.005)
        at = time.monotonic() + lead if lead > 0 else None
        try:
            done = await self.ctx.device_call(self.ctx.instrument(), remote.set, channel, value, at)
        except Exception as error:  # noqa: BLE001 - the device refused or did not answer
            raise NodeError(f"{self.node.id}: {error}") from None
        self.ctx.emit("done", signals.Event(name=channel, times=[self.flow_time(done)], data=[value]))


@node("remote.call", title="Call",
      description="Calls a command of a remote device with 'args' for every value at 'trigger' (once at the "
                  "start without a wire); 'result' is what it answers.",
      inputs=[DEVICE_INPUT, In("trigger", signals.ANY)],
      outputs=[Out("result", signals.ANY), Out("done", signals.EVENT)],
      params=[Param("command", "str", required=True, suggest="remote_commands"), Param("args", "dict", {}),
              Param("timeout", "quantity", "10 s", "s")],
      icon="terminal")
class CallNode(_RemoteNode):
    async def run(self) -> None:
        self.remote()
        if not self.ctx.wired("trigger"):
            await self.call()

    async def on_input(self, port: str, value: Any) -> None:
        await self.call()

    async def call(self) -> None:
        remote = self.remote()
        command = str(self.p("command") or "")
        if command not in remote.commands():
            raise NodeError(f"{self.node.id}: {remote.device_name} has no command {command!r} "
                            f"(commands: {', '.join(remote.commands()) or 'none'})")
        try:
            result = await self.ctx.device_call(self.ctx.instrument(), remote.call, command,
                                                dict(self.p("args") or {}), self.q("timeout") or 10.0)
        except TypeError as error:  # the command's own arguments did not fit
            raise NodeError(f"{command}: the arguments {dict(self.p('args') or {})} do not fit ({error})") from None
        except Exception as error:  # noqa: BLE001 - reported at the node
            raise NodeError(str(error)) from None
        self.ctx.emit("result", result)
        self.ctx.emit("done", signals.Event(name=command, times=[self.ctx.now()], data=[result]))


#: edges of the device and of the recording that are kept for the fit
SYNC_WINDOW = 64
#: how far apart device and recording may be when the edges are matched by their pattern (s)
SYNC_SEARCH = 2.0


@node("remote.sync", title="Sync signal",
      description="Aligns the clock of a remote device with a sync signal: the device reports the times of "
                  "the signal's edges in its clock (a sync output it drives, a sync input it sees, or an input "
                  "whose blocks record it). 'signal' is the same signal recorded by a channel of another "
                  "instrument (a capture or stream); 'edges' instead takes the edge times of timing.sync (as "
                  "precise as its output). The matched edges give offset and drift with the precision of the "
                  "recording (drift: auto fits it unless the device's sample clock is shared; none: only the "
                  "offset); from then on the device's values are converted with them. 'offset' is the device "
                  "time minus the local time, 'uncertainty' its scatter.",
      inputs=[DEVICE_INPUT, In("signal", signals.DIGITAL, "the recorded sync signal"),
              In("edges", signals.EVENT, "edge times of the sync signal (timing.sync)")],
      outputs=[Out("offset", signals.SCALAR), Out("uncertainty", signals.SCALAR), Out("edges", signals.SCALAR)],
      params=[Param("sync", "str", required=True, suggest="remote_syncs"),
              Param("drift", "choice", "auto", choices=("auto", "fit", "none"),
                    description="auto: fitted unless the sample clock is shared; none: offset only")],
      icon="clock")
class SyncNode(_RemoteNode):
    async def setup(self) -> None:
        self.remote_edges: list[tuple] = []
        self.local_edges: list[tuple[float, Optional[int]]] = []
        #: local time -> (local time, the device's edge: (device time, level[, sample index]))
        self.pairs: dict[float, tuple[float, tuple]] = {}
        self.lock = threading.Lock()
        self.stop_listening = None

    async def run(self) -> None:
        remote = self.remote()
        name = str(self.p("sync") or "")
        if name not in remote.syncs():
            raise NodeError(f"{self.node.id}: {remote.device_name} has no sync signal {name!r} "
                            f"(sync signals: {', '.join(remote.syncs()) or 'none'})")
        if not self.ctx.wired("signal") and not self.ctx.wired("edges"):
            raise NodeError(f"{self.node.id}: wire 'signal' (the recorded sync signal) or 'edges' (timing.sync)")

        def edges(items: list) -> None:
            with self.lock:
                self.remote_edges = (self.remote_edges + list(items))[-4 * SYNC_WINDOW:]

        self.stop_listening = remote.listen_sync(name, edges)

    def fit_drift(self, remote: RemoteFacet) -> bool:
        drift = str(self.p("drift", "auto") or "auto")
        if drift == "auto":
            return (remote.syncs().get(str(self.p("sync") or "")) or {}).get("clock") != "shared"
        return drift == "fit"

    async def on_input(self, port: str, value: Any) -> None:
        if port == "signal":
            if not isinstance(value, signals.Digital) or not value.time.is_known or len(value) < 2:
                return
            levels = value.values.astype("int8")
            index = [int(i) + 1 for i in (levels[1:] != levels[:-1]).nonzero()[0]]
            times = value.time.times(len(value))
            new = [(self.local_time(float(times[position])), int(levels[position])) for position in index]
        elif port == "edges" and isinstance(value, signals.Event):
            levels = [int(item) if isinstance(item, (int, bool)) else None for item in value.data]
            new = [(self.local_time(float(at)), level) for at, level in zip(value.times, levels)]
        else:
            return
        if not new:
            return
        remote = self.remote()
        clock = remote.clock()
        name = str(self.p("sync") or "")

        def device_time(edge: tuple) -> float:
            """An edge of an input recording the signal is placed again with all the device knows now."""
            if len(edge) > 2:
                again = remote.block_time(name, edge[2])
                if again is not None:
                    return again
            return edge[0]

        with self.lock:
            device_edges = [(device_time(edge), edge[1], edge) for edge in self.remote_edges]
            self.local_edges = (self.local_edges + new)[-4 * SYNC_WINDOW:]
            local_edges = list(self.local_edges)
        if len(device_edges) < 2:
            return
        gaps = [b[0] - a[0] for a, b in itertools.pairwise(device_edges) if b[0] > a[0]]
        tolerance = 0.4 * min(gaps) if gaps else 0.005
        pair_edges(local_edges, device_edges, clock.to_remote, tolerance, self.pairs)
        if len(self.pairs) < 4 and len(local_edges) >= 6 and len(device_edges) >= 6:
            # the clock is far off (not measured yet, a wrong time scale): match the pattern of the edges
            found = timing.match(np.array([local for local, _level in local_edges]),
                                 np.array([clock.to_local(at) for at, _level, _edge in device_edges]), SYNC_SEARCH)
            by_local = {round(clock.to_local(at), 9): edge for at, _level, edge in device_edges}
            for device_local, local in found:
                edge = by_local.get(round(device_local, 9))
                if edge is not None:
                    self.pairs[round(local, 9)] = (local, edge)
        kept = sorted(self.pairs.values(), key=lambda pair: pair[0])[-SYNC_WINDOW:]
        self.pairs = {round(local, 9): (local, edge) for local, edge in kept}
        pairs = [(local, device_time(edge)) for local, edge in kept]
        if len(pairs) < 4:
            return
        scatter = clock.set_signal(pairs, fit_drift=self.fit_drift(remote))
        now = time.monotonic()
        offset = clock.to_remote(now) - now
        at = self.ctx.now()
        self.ctx.emit("offset", signals.Scalar(name="offset", unit="s", value=offset, at=at))
        self.ctx.emit("uncertainty", signals.Scalar(name="uncertainty", unit="s", value=scatter, at=at))
        self.ctx.emit("edges", signals.Scalar(name="edges", value=float(len(pairs)), at=at))

    def cleanup(self) -> None:
        if self.stop_listening is not None:
            self.stop_listening()
            self.stop_listening = None


def pair_edges(local_edges: list, device_edges: list, to_remote, tolerance: float, pairs: dict) -> None:
    """Pairs every local edge ``(time, level)`` not in ``pairs`` yet (the device's may come later)
    with the nearest device edge ``(device time, level, edge)`` of its level within ``tolerance``.
    Vectorized: a sync node runs it for every block of a recording."""
    waiting = [(local, level) for local, level in local_edges if round(local, 9) not in pairs]
    if not waiting or not device_edges:
        return
    by_level: dict = {}
    for edge in device_edges:
        for key in (edge[1], None):  # (a local edge of unknown level takes any)
            by_level.setdefault(key, []).append(edge)
    for level, group in by_level.items():
        mine = [(local, wanted) for local, wanted in waiting if wanted == level]
        if not mine:
            continue
        group.sort(key=lambda edge: edge[0])
        times = np.array([edge[0] for edge in group])
        predicted = np.array([to_remote(local) for local, _level in mine])
        position = np.clip(np.searchsorted(times, predicted), 1, max(len(times) - 1, 1))
        before = np.clip(position - 1, 0, len(times) - 1)
        after = np.clip(position, 0, len(times) - 1)
        nearest = np.where(np.abs(times[before] - predicted) <= np.abs(times[after] - predicted), before, after)
        close = np.abs(times[nearest] - predicted) <= tolerance
        for (local, _level), index, ok in zip(mine, nearest, close):
            if ok:
                pairs[round(local, 9)] = (local, group[int(index)][2])


def scalar_value(value: Any, unit: str) -> float:
    """A number of the device as openSciLab keeps it: percent as a share (45 % is 0.45)."""
    return float(value) / 100.0 if unit == "%" else float(value)


def _plain(value: Any) -> Any:
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return value.value
    if isinstance(value, signals.Event):
        return value.data[-1] if len(value.data) else None
    return value


def _truth(value: Any) -> bool:
    """A truth value: true/false, on/off, yes/no, 1/0 as text too (a number: 0.5 or more)."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "on", "yes", "high"):
            return True
        if text in ("false", "off", "no", "low", ""):
            return False
        return float(text) >= 0.5
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) >= 0.5
    return bool(value)


NODES = [cls.node_spec for cls in (ReceiveNode, SetNode, CallNode, SyncNode)]
