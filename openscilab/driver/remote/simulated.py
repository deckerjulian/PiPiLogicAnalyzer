# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Simulated remote devices (``remote-sim:<demo>``): the demo devices of ``openscilab_device.demo``
in a thread of the application, as a script on another computer would be:

* with a clock of their own - an offset of minutes and a drift (50 ppm by default) - so the time
  conversion has something to correct;
* behind an emulated network (:class:`NetworkEmulator`): every chunk of data waits a latency plus
  a random jitter, in order (as TCP delivers it);
* with their sync output on a net other simulators can be wired to (``simulation.wiring`` of the
  project: ``{la: [{from: "station:SYNC", to: D5}]}``), and nets of other simulators wired to their
  inputs (``{daq: [{from: "uno:D7", to: SYNC_IN}]}``: the DAQ records the Uno's pin D7).

``remote-sim:climate``, ``remote-sim:audio``, ``remote-sim:echo``, ``remote-sim:daq``; ``#2`` makes a
second one. Options after ``?`` (``remote-sim:daq#2?latency=20&drift=200``): ``timescale=utc`` (the
device's clock follows UTC, as with PTP), ``shared_clock=1`` (the DAQ's sample clock is that of
openSciLab's instruments), ``latency`` and ``jitter`` of the network (milliseconds, one way),
``drift`` of the device's clock (ppm). Latency, jitter and drift also change while it runs
(:meth:`SimulatedRemote.configure`, the *Simulation* box of its device card).
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import socket
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Optional

from openscilab_device import demo


@dataclass
class Network:
    """How the emulated network behaves (seconds, one way)."""

    latency: float = 0.004
    jitter: float = 0.002


@dataclass
class DeviceClock:
    """The clock of a simulated device: ``offset + (1 + drift) * time.monotonic()``."""

    offset: float = 1000.0
    drift: float = 50e-6

    def __call__(self) -> float:
        return self.offset + (1.0 + self.drift) * time.monotonic()

    def to_local(self, remote: float) -> float:
        return (remote - self.offset) / (1.0 + self.drift)


class NetworkEmulator:
    """A TCP relay on localhost that delays what passes in both directions."""

    def __init__(self, target: tuple[str, int], network: Network, seed: int = 1) -> None:
        self.target, self.network = target, network
        self.random = random.Random(seed)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(4)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._accept, name="openscilab-netem", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _address = self._socket.accept()
            except OSError:
                return
            try:
                server = socket.create_connection(self.target, timeout=5.0)
            except OSError:
                client.close()
                continue
            for sock in (client, server):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(None)
            for source, sink in ((client, server), (server, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    def _delay(self) -> float:
        return max(self.network.latency + self.random.uniform(-1, 1) * self.network.jitter, 0.0)

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        queue: list = []
        lock = threading.Condition()
        order = [0]
        last = [0.0]

        def forward() -> None:
            while True:
                with lock:
                    while not queue:
                        lock.wait()
                    due, _index, data = queue[0]
                    wait = due - time.monotonic()
                    if wait > 0:
                        lock.wait(wait)
                        continue
                    heapq.heappop(queue)
                if data is None:
                    try:
                        sink.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                try:
                    sink.sendall(data)
                except OSError:
                    return

        threading.Thread(target=forward, daemon=True).start()
        while not self._stop.is_set():
            try:
                data = source.recv(65536)
            except OSError:
                data = b""
            with lock:
                # in order, as TCP: a chunk never overtakes the one before
                due = max(time.monotonic() + self._delay(), last[0])
                last[0] = due
                order[0] += 1
                heapq.heappush(queue, (due, order[0], data or None))
                lock.notify()
            if not data:
                return


class SimulatedRemote:
    """A running demo device; :meth:`close` stops it, :meth:`configure` changes its network and the
    drift of its clock while it runs."""

    def __init__(self, name: str, demo_name: str, server_address: tuple[str, int], network: Network,
                 clock: Callable[[], float], circuit_clock: Callable[[], float], seed: int = 1,
                 options: Optional[dict] = None) -> None:
        from ..simulated.circuit import Circuit, OutputSource, Sine

        options = dict(options or {})
        self.name, self.network, self.clock = name, network, clock
        self.netem = NetworkEmulator(server_address, network, seed)
        #: the circuit of the device: its outputs for wires to other simulators, the nets its inputs
        #: read (wired from other simulators)
        outputs = {net: OutputSource(high=3.3) for net in ("SYNC", "DO0")}
        circuit = Circuit()
        for net, output in outputs.items():
            output.set(circuit_clock(), 0)
            circuit.drive(net, output)
        if demo_name == "daq":
            # what the standalone demo measures on AI0 (a wire from another simulator replaces it)
            circuit.drive("AI0", Sine(frequency=50.0, amplitude=1.0))
        self.wiring_source = SimpleNamespace(circuit=circuit, clock=circuit_clock, profile={"logic_level": 3.3})

        def write_sync(level: int) -> None:
            outputs["SYNC"].add(circuit_clock(), int(level))

        def to_circuit(at: float) -> float:
            """A time of ``time.monotonic`` on the clock of the circuit."""
            return at + (circuit_clock() - time.monotonic())

        def read(net: str, start: float, rate: float, count: int, analog: bool):
            begin = to_circuit(start)
            if net not in circuit.sources:
                return [0.0] * count if analog else [0] * count
            return (circuit.analog(net, begin, rate, count) if analog else circuit.digital(net, begin, rate, count))

        def write(net: str, level: int, at: float) -> None:
            if net in outputs:
                outputs[net].add(to_circuit(at), int(level))

        timescale = options.get("timescale")
        extra = {"timescale": timescale, "timescale_accuracy": 2e-5} if timescale else {}
        self.device, drive = demo.build(demo_name, write_sync=write_sync, server=f"127.0.0.1:{self.netem.port}",
                                        clock=clock, read=read, write=write,
                                        shared_clock=str(options.get("shared_clock", "")) in ("1", "true", "yes"),
                                        **extra)
        self.device.name = name
        self.device.start()
        self._driver = threading.Thread(target=drive, name=f"openscilab-remote-sim-{name}", daemon=True)
        self._driver.start()

    def close(self) -> None:
        self.device.stop()
        self.netem.close()

    def settings(self) -> dict:
        """Latency and jitter of the network (seconds, one way), the drift of the clock (``None``:
        it follows a time scale and has none to change)."""
        drift = self.clock.drift if isinstance(self.clock, DeviceClock) else None
        return {"latency": self.network.latency, "jitter": self.network.jitter, "drift": drift}

    def configure(self, latency: Optional[float] = None, jitter: Optional[float] = None,
                  drift: Optional[float] = None) -> dict:
        """Change the network (seconds, one way) and the drift of the clock, at once. The clock keeps
        its time at this moment (no jump), as a real oscillator that gets warmer would."""
        if latency is not None:
            if not 0 <= latency <= 5.0:
                raise ValueError("latency: 0 to 5 s")
            self.network.latency = float(latency)
        if jitter is not None:
            if not 0 <= jitter <= 5.0:
                raise ValueError("jitter: 0 to 5 s")
            self.network.jitter = float(jitter)
        if drift is not None and isinstance(self.clock, DeviceClock):
            if not -0.01 <= drift <= 0.01:
                raise ValueError("drift: at most 10000 ppm")
            now = time.monotonic()
            before = self.clock()
            self.clock.drift = float(drift)
            self.clock.offset = before - (1.0 + self.clock.drift) * now
        return self.settings()


def describe(demo_name: str, options: Optional[dict] = None) -> dict:
    """The description of a demo device (what the inspector offers before it runs)."""
    shared = str((options or {}).get("shared_clock", "")) in ("1", "true", "yes")
    device, _drive = demo.build(demo_name, shared_clock=shared)
    return device.describe()


#: numbers the simulated devices (their names must differ, also after one was closed)
_numbers = itertools.count(1)

#: options of an address (``remote-sim:daq?timescale=utc``) and their values
OPTIONS = {"timescale": ("utc", "tai"), "shared_clock": ("0", "1", "true", "false", "yes", "no")}
#: options with a number (``latency=20``): unit, lowest and highest value
NUMBERS = {"latency": ("ms", 0.0, 5000.0), "jitter": ("ms", 0.0, 5000.0), "drift": ("ppm", -10_000.0, 10_000.0)}


def address(demo_name: str, instance: int = 1, options: Optional[dict] = None) -> str:
    """The address of a simulated remote device (the reverse of :func:`parse`)."""
    text = f"remote-sim:{demo_name}" + (f"#{instance}" if instance > 1 else "")
    query = "&".join(f"{key}={value:g}" if isinstance(value, float) else f"{key}={value}"
                     for key, value in (options or {}).items())
    return text + (f"?{query}" if query else "")


def parse(address: str) -> tuple[str, int, dict]:
    """``remote-sim:climate#2`` -> ``("climate", 2, {})``; ``remote-sim:daq?timescale=utc`` ->
    ``("daq", 1, {"timescale": "utc"})``."""
    rest = address.split(":", 1)[1] if ":" in address else address
    rest, _, query = rest.partition("?")
    name, _, instance = rest.partition("#")
    name = name.strip() or "climate"
    if name not in demo.DEMOS:
        raise ValueError(f"no simulated remote device {name!r} (remote-sim:{', remote-sim:'.join(demo.DEMOS)})")
    options = {}
    for part in filter(None, query.split("&")):
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip().lower()
        if key in NUMBERS:
            unit, low, high = NUMBERS[key]
            try:
                number = float(value)
            except ValueError:
                number = math.nan
            if not low <= number <= high:
                raise ValueError(f"{address}: {key} is a number of {unit} from {low:g} to {high:g}")
            options[key] = number
            continue
        if key not in OPTIONS or value not in OPTIONS[key]:
            raise ValueError(f"{address}: option {part!r} (options: "
                             + ", ".join(f"{name}={'|'.join(values)}" for name, values in OPTIONS.items())
                             + ", " + ", ".join(f"{name}=<{unit}>" for name, (unit, _low, _high) in NUMBERS.items())
                             + ")")
        options[key] = value
    return name, int(instance) if instance.strip() else 1, options


def open_simulated_remote(address: str, circuit_clock: Optional[Callable[[], float]] = None,
                          network: Optional[Network] = None, clock: Optional[DeviceClock] = None,
                          seed: int = 1, timeout: float = 10.0):
    """An instrument of the simulated remote device at ``address`` (connected to the local server)."""
    from . import servers
    from .instrument import make_instrument

    demo_name, instance, options = parse(address)
    name = demo_name if instance == 1 else f"{demo_name}{instance}"
    rng = random.Random(seed * 7919 + instance)
    if clock is None:
        if options.get("timescale"):
            # a clock that follows the time scale (as PTP keeps it), 15 µs off
            from openscilab_device.timing import timescale_now

            timescale = options["timescale"]
            clock = lambda: timescale_now(timescale) + 15e-6  # noqa: E731
        else:
            clock = DeviceClock(offset=rng.uniform(100.0, 10_000.0), drift=options.get("drift", 50.0) * 1e-6)
    if network is None:
        network = Network()
        network.latency = options.get("latency", network.latency * 1e3) / 1e3
        network.jitter = options.get("jitter", network.jitter * 1e3) / 1e3
    server = servers.local_server()
    simulated = SimulatedRemote(f"sim-{name}-{next(_numbers)}", demo_name, ("127.0.0.1", server.port),
                                network, clock, circuit_clock or time.monotonic, seed, options)
    try:
        connection = server.wait_for(simulated.device.name, timeout)
    except Exception:
        simulated.close()
        raise
    instrument = make_instrument(server, connection.name, uri=address, kind=f"Simulation: remote {demo_name}",
                                 simulated=simulated)
    return instrument
