# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The device hub: every instrument of the project open at the same time.

The hub keeps the instruments by name, reports changes (opened, closed, status) to its
subscribers, knows the *trigger routes* (a trigger output of one instrument wired to the trigger
input of another, with the cable that is needed) and the *time base offsets* of the streams:
each instrument has its own clock, and a stream's offset to the common time base comes with its
origin:

* ``trigger_line`` – exact: the instruments share a trigger cable (a route);
* ``reference_edge`` – measured: the same signal was recorded by both (:func:`reference_offset`);
* ``estimated`` – coarse: software time stamps of the computer, marked as such.

The module has no Qt dependency; the shell forwards the events to the user interface.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .instrument import Instrument, InstrumentStatus

log = logging.getLogger(__name__)

ORIGIN_TRIGGER_LINE = "trigger_line"
ORIGIN_REFERENCE_EDGE = "reference_edge"
ORIGIN_ESTIMATED = "estimated"
OFFSET_ORIGINS = (ORIGIN_TRIGGER_LINE, ORIGIN_REFERENCE_EDGE, ORIGIN_ESTIMATED)

EVENT_ADDED = "added"
EVENT_REMOVED = "removed"
EVENT_STATUS = "status"
EVENT_ROUTES = "routes"
EVENT_OFFSET = "offset"


class HubError(ValueError):
    pass


@dataclass(frozen=True)
class HubEvent:
    kind: str
    name: str = ""
    status: Optional[InstrumentStatus] = None
    message: str = ""
    #: the instrument that was added or removed (a removed one is no longer in the hub)
    instrument: Optional[Instrument] = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class TriggerRoute:
    """``source``'s trigger output wired to ``target``'s trigger input."""

    source: str
    output: str
    target: str
    input: str
    #: Delay of the cable and the input in seconds (added to the offset of the target).
    delay: float = 0.0

    @property
    def cable(self) -> str:
        """What has to be wired, for people."""
        return f"Connect {self.output} of {self.source} to {self.input} of {self.target}"


@dataclass(frozen=True)
class StreamOffset:
    """Where a stream sits on the common time base: its time 0 is ``offset`` seconds of the hub."""

    offset: float
    origin: str = ORIGIN_ESTIMATED
    #: The stream or route it was measured against
    reference: str = ""

    def __post_init__(self) -> None:
        if self.origin not in OFFSET_ORIGINS:
            raise HubError(f"unknown origin {self.origin!r}")

    @property
    def exact(self) -> bool:
        return self.origin == ORIGIN_TRIGGER_LINE

    def describe(self) -> str:
        how = {
            ORIGIN_TRIGGER_LINE: "trigger line (exact)",
            ORIGIN_REFERENCE_EDGE: "reference edge (measured)",
            ORIGIN_ESTIMATED: "software time stamps (estimated)",
        }[self.origin]
        return f"{self.offset * 1e6:+.3f} µs, {how}"


HubListener = Callable[[HubEvent], None]


def reference_offset(times_a: np.ndarray, times_b: np.ndarray, max_shift: float,
                     minimum_edges: int = 3, tolerance: Optional[float] = None) -> Optional[float]:
    """Seconds to add to the times of ``b`` so its edges fall onto the edges of ``a``.

    Both arrays hold the times of the edges of the same signal recorded by two instruments (each
    on its own clock). Edges closer than ``tolerance`` (default: 5 % of the median distance of the
    edges of ``a``) count as the same edge. ``None`` when too few edges match within
    ``max_shift``. A periodic signal is ambiguous by whole periods: ``max_shift`` has to be
    smaller than half a period, or the signal irregular.
    """
    a = np.sort(np.asarray(times_a, dtype=np.float64))
    b = np.sort(np.asarray(times_b, dtype=np.float64))
    if len(a) < minimum_edges or len(b) < minimum_edges:
        return None
    if tolerance is None:
        tolerance = 0.05 * float(np.median(np.diff(a))) if len(a) > 1 else max_shift * 1e-3
    best: Optional[tuple[int, float]] = None
    # Candidate shifts: every pairing of the first edges of b with edges of a near them.
    for edge in b[: min(len(b), 8)]:
        low = np.searchsorted(a, edge - max_shift)
        high = np.searchsorted(a, edge + max_shift, side="right")
        for candidate in a[low:high]:
            shift = candidate - edge
            moved = b + shift
            index = np.clip(np.searchsorted(a, moved), 1, len(a) - 1)
            nearest = np.minimum(np.abs(a[index - 1] - moved), np.abs(a[index] - moved))
            matches = int(np.count_nonzero(nearest <= tolerance))
            if best is None or matches > best[0] or (matches == best[0] and abs(shift) < abs(best[1])):
                best = (matches, shift)
    if best is None or best[0] < minimum_edges:
        return None
    # Refine: median of the residuals of the matched edges.
    shift = best[1]
    moved = b + shift
    index = np.clip(np.searchsorted(a, moved), 1, len(a) - 1)
    before, after = a[index - 1] - moved, a[index] - moved
    nearest = np.where(np.abs(before) <= np.abs(after), before, after)
    close = nearest[np.abs(nearest) <= tolerance]
    return float(shift + (np.median(close) if close.size else 0.0))


class Hub:
    """Instruments by name, trigger routes and stream offsets; thread safe."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._instruments: dict[str, Instrument] = {}
        self._routes: list[TriggerRoute] = []
        self._offsets: dict[str, StreamOffset] = {}
        self._listeners: list[HubListener] = []
        self._lock = threading.RLock()
        #: The time of the hub in seconds (real time; the engine sets a virtual clock).
        self.clock: Callable[[], float] = clock or time.monotonic

    # -------------------------------------------------------------- events
    def subscribe(self, listener: HubListener) -> Callable[[], None]:
        """Call ``listener`` for every :class:`HubEvent`; returns a function that unsubscribes."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: HubEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - one listener must not break the hub or the others
                log.exception("A listener of the device hub failed on %s %s", event.kind, event.name)

    def now(self) -> float:
        return self.clock()

    def set_clock(self, clock: Callable[[], float]) -> None:
        self.clock = clock

    # --------------------------------------------------------- instruments
    def unique_name(self, name: str) -> str:
        with self._lock:
            if name not in self._instruments:
                return name
            number = 2
            while f"{name} {number}" in self._instruments:
                number += 1
            return f"{name} {number}"

    def add(self, instrument: Instrument, name: Optional[str] = None) -> str:
        """Open ``instrument`` in the hub under ``name`` (made unique); returns the name."""
        with self._lock:
            if instrument in self._instruments.values():
                return self.name_of(instrument)
            name = self.unique_name(name or instrument.name)
            instrument.name = name
            self._instruments[name] = instrument
        self._emit(HubEvent(EVENT_ADDED, name, instrument.status, instrument=instrument))
        return name

    def remove(self, name: str, close: bool = True) -> Optional[Instrument]:
        """Take ``name`` out of the hub (and close it); its routes and offsets go with it."""
        with self._lock:
            instrument = self._instruments.pop(name, None)
            if instrument is None:
                return None
            dropped = [route for route in self._routes if name in (route.source, route.target)]
            self._routes = [route for route in self._routes if route not in dropped]
            for route in dropped:
                current = self._offsets.get(route.target)
                if current is not None and current.origin == ORIGIN_TRIGGER_LINE:
                    del self._offsets[route.target]
            self._offsets = {key: value for key, value in self._offsets.items()
                             if key != name and not key.startswith(f"{name}/")}
        if close:
            instrument.close()
        self._emit(HubEvent(EVENT_REMOVED, name, InstrumentStatus.DISCONNECTED, instrument=instrument))
        return instrument

    def get(self, name: str) -> Instrument:
        with self._lock:
            try:
                return self._instruments[name]
            except KeyError:
                raise HubError(f"no instrument {name!r} in the hub") from None

    def find(self, name: str) -> Optional[Instrument]:
        with self._lock:
            return self._instruments.get(name)

    def name_of(self, instrument: Instrument) -> str:
        with self._lock:
            for name, candidate in self._instruments.items():
                if candidate is instrument:
                    return name
        raise HubError(f"{instrument!r} is not in the hub")

    def instruments(self) -> list[Instrument]:
        with self._lock:
            return list(self._instruments.values())

    def names(self) -> list[str]:
        with self._lock:
            return list(self._instruments)

    def __contains__(self, item) -> bool:
        with self._lock:
            return item in self._instruments or item in self._instruments.values()

    def __len__(self) -> int:
        return len(self._instruments)

    def with_facet(self, facet_class) -> list[Instrument]:
        return [instrument for instrument in self.instruments() if instrument.has(facet_class)]

    def set_status(self, name: str, status: InstrumentStatus, message: str = "") -> None:
        instrument = self.get(name)
        instrument.status = status
        self._emit(HubEvent(EVENT_STATUS, name, status, message))

    def close_all(self) -> None:
        for name in self.names():
            self.remove(name)

    # ------------------------------------------------------- trigger routes
    def add_route(self, source: str, output: str, target: str, input: str, delay: float = 0.0) -> TriggerRoute:
        """Wire ``source.output`` to ``target.input``; the target's stream offset becomes exact."""
        source_instrument, target_instrument = self.get(source), self.get(target)
        if source == target:
            raise HubError("a trigger route connects two instruments")
        if output not in source_instrument.trigger_outputs:
            raise HubError(f"{source} has no trigger output {output!r} "
                           f"(it has: {', '.join(source_instrument.trigger_outputs) or 'none'})")
        if input not in target_instrument.trigger_inputs:
            raise HubError(f"{target} has no trigger input {input!r} "
                           f"(it has: {', '.join(target_instrument.trigger_inputs) or 'none'})")
        route = TriggerRoute(source, output, target, input, delay)
        with self._lock:
            self._routes = [existing for existing in self._routes
                            if (existing.target, existing.input) != (target, input)]
            self._routes.append(route)
            base = self._offsets.get(source, StreamOffset(0.0, ORIGIN_TRIGGER_LINE, source))
            self._offsets[target] = StreamOffset(base.offset + delay, ORIGIN_TRIGGER_LINE, route.cable)
        self._emit(HubEvent(EVENT_ROUTES, target))
        return route

    def remove_route(self, route: TriggerRoute) -> None:
        with self._lock:
            if route in self._routes:
                self._routes.remove(route)
                if self._offsets.get(route.target, StreamOffset(0)).origin == ORIGIN_TRIGGER_LINE:
                    self._offsets.pop(route.target, None)
        self._emit(HubEvent(EVENT_ROUTES, route.target))

    def routes(self) -> list[TriggerRoute]:
        with self._lock:
            return list(self._routes)

    def routes_from(self, source: str) -> list[TriggerRoute]:
        return [route for route in self.routes() if route.source == source]

    # -------------------------------------------------------- stream offsets
    def set_offset(self, stream: str, offset: float, origin: str = ORIGIN_ESTIMATED, reference: str = "") -> StreamOffset:
        """Place ``stream`` (an instrument name or ``instrument/stream``) on the common time base."""
        value = StreamOffset(float(offset), origin, reference)
        with self._lock:
            current = self._offsets.get(stream)
            # A measured or exact offset is not replaced by a coarser estimate.
            rank = {ORIGIN_TRIGGER_LINE: 0, ORIGIN_REFERENCE_EDGE: 1, ORIGIN_ESTIMATED: 2}
            if current is not None and rank[current.origin] < rank[origin]:
                return current
            self._offsets[stream] = value
        self._emit(HubEvent(EVENT_OFFSET, stream))
        return value

    def offset(self, stream: str) -> StreamOffset:
        """Offset of ``stream``; a stream of an instrument inherits the instrument's offset."""
        with self._lock:
            if stream in self._offsets:
                return self._offsets[stream]
            instrument = stream.split("/", 1)[0]
            if instrument in self._offsets:
                return self._offsets[instrument]
        return StreamOffset(0.0, ORIGIN_ESTIMATED)

    def to_common_time(self, stream: str, times):
        """Times of ``stream`` (its own clock) on the common time base."""
        return np.asarray(times, dtype=np.float64) + self.offset(stream).offset

    def stamp(self, stream: str, start_of_stream: float) -> StreamOffset:
        """Coarse offset from a software time stamp: the stream started at ``start_of_stream``
        seconds of the hub clock."""
        return self.set_offset(stream, start_of_stream, ORIGIN_ESTIMATED, "software time stamp")

    def align_by_reference(self, reference: str, reference_edges, stream: str, stream_edges,
                           max_shift: float) -> Optional[StreamOffset]:
        """Measure the offset of ``stream`` against ``reference`` from edges of one signal both saw."""
        shift = reference_offset(self.to_common_time(reference, reference_edges), np.asarray(stream_edges), max_shift)
        if shift is None:
            return None
        with self._lock:
            current = self._offsets.get(stream)
            if current is not None and current.origin == ORIGIN_TRIGGER_LINE:
                return current
            self._offsets[stream] = StreamOffset(shift, ORIGIN_REFERENCE_EDGE, reference)
        self._emit(HubEvent(EVENT_OFFSET, stream))
        return self._offsets[stream]
