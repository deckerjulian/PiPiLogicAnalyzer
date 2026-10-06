# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A remote device as an instrument of the lab.

:class:`RemoteFacet` is the device as it describes itself: its inputs (values with their time in
local time), outputs (set at a time), commands, sync signals and the state of its clock. The
other facets let the nodes that already exist use it: a ``bool`` output is a pin of
:class:`RemoteGpio` (``gpio.write``), ``scalar``/``bool`` inputs are read by :class:`RemoteMonitor`
(``device.monitor``) and :class:`RemoteAnalogIn` (``gpio.read`` with ``analog``).

The facets look the connection up by the device's name every time: a device that connects again
(after a network drop) is the same instrument.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from ...core.instrument import (
    MODE_INPUT,
    MODE_OUTPUT,
    PIN_DIN,
    PIN_DOUT,
    AnalogInFacet,
    Facet,
    GpioFacet,
    Instrument,
    InstrumentError,
    InstrumentStatus,
    MonitorFacet,
    MonitorState,
    PinInfo,
)
from .clock import ClockState
from .server import Received, RemoteConnection, RemoteError, RemoteServer, syncs_of


class RemoteFacet(Facet):
    """Inputs, outputs, commands and sync signals of a remote device."""

    title = "Remote device"

    def __init__(self, server: RemoteServer, name: str, simulated: Any = None) -> None:
        super().__init__()
        self.server, self.device_name, self.simulated = server, name, simulated
        self._description: dict = {}
        connection = server.device(name)
        if connection is not None:
            self._description = connection.description

    @property
    def connection(self) -> RemoteConnection:
        connection = self.server.device(self.device_name)
        if connection is None:
            raise InstrumentError(f"the remote device {self.device_name} is not connected")
        self._description = connection.description
        return connection

    @property
    def connected(self) -> bool:
        return self.server.device(self.device_name) is not None

    @property
    def description(self) -> dict:
        """What the device said about itself (the last time it was connected)."""
        connection = self.server.device(self.device_name)
        if connection is not None:
            self._description = connection.description
        return self._description

    def _section(self, key: str) -> dict[str, dict]:
        return {item["name"]: item for item in self.description.get(key) or []}

    def inputs(self) -> dict[str, dict]:
        return self._section("inputs")

    def outputs(self) -> dict[str, dict]:
        return self._section("outputs")

    def commands(self) -> dict[str, dict]:
        return self._section("commands")

    def syncs(self) -> dict[str, dict]:
        return syncs_of(self.description)

    def listen(self, channel: str, listener: Callable[[Received], None]) -> Callable[[], None]:
        try:
            return self.connection.listen(channel, listener)
        except RemoteError as error:
            raise InstrumentError(str(error)) from None

    def listen_sync(self, name: str, listener: Callable[[list], None]) -> Callable[[], None]:
        try:
            return self.connection.listen_sync(name, listener)
        except RemoteError as error:
            raise InstrumentError(str(error)) from None

    def set(self, channel: str, value: Any, at: Optional[float] = None, timeout: float = 5.0) -> float:
        """Set an output at the local time ``at`` (``None``: at once); the local time it happened."""
        try:
            return self.connection.set(channel, value, at, timeout)
        except RemoteError as error:
            raise InstrumentError(str(error)) from None

    def call(self, name: str, args: Optional[dict] = None, timeout: float = 10.0) -> Any:
        try:
            return self.connection.call(name, args, timeout)
        except RemoteError as error:
            raise InstrumentError(str(error)) from None

    def block_time(self, channel: str, index: float) -> Optional[float]:
        connection = self.server.device(self.device_name)
        return connection.block_time(channel, index) if connection is not None else None

    def latest(self, channel: str) -> Optional[Received]:
        connection = self.server.device(self.device_name)
        return connection.latest.get(channel) if connection is not None else None

    def clock_state(self) -> Optional[ClockState]:
        connection = self.server.device(self.device_name)
        return connection.clock.state(time.monotonic()) if connection is not None else None

    def clock(self):
        return self.connection.clock

    def typical_delay(self) -> float:
        connection = self.server.device(self.device_name)
        return connection.clock.typical_delay() if connection is not None else 0.05

    def close(self) -> None:
        if self.simulated is not None:
            self.simulated.close()
            self.simulated = None


class RemoteGpio(GpioFacet):
    """``bool`` outputs as output pins, ``bool`` inputs as input pins."""

    keepalive = False  # the connection pings by itself

    def __init__(self, remote: RemoteFacet) -> None:
        super().__init__()
        self.remote = remote

    def pins(self) -> list[PinInfo]:
        pins = [PinInfo(name, frozenset({PIN_DOUT}), description=item.get("description", ""))
                for name, item in self.remote.outputs().items() if item["kind"] == "bool"]
        pins += [PinInfo(name, frozenset({PIN_DIN}), description=item.get("description", ""))
                 for name, item in self.remote.inputs().items() if item["kind"] == "bool"]
        return pins

    def set_mode(self, pin: str, mode: str) -> None:
        self.pin(pin)  # a pin of the device: its direction is given

    def mode(self, pin: str) -> str:
        return MODE_OUTPUT if pin in self.remote.outputs() else MODE_INPUT

    def write(self, pin: str, value: int) -> None:
        if pin not in self.remote.outputs():
            raise InstrumentError(f"{pin} is no output of {self.remote.device_name}")
        self.remote.set(pin, bool(value))

    def read(self, pin: str) -> int:
        latest = self.remote.latest(pin)
        if latest is None:
            raise InstrumentError(f"{self.remote.device_name} has not sent {pin} yet")
        return 1 if latest.value else 0

    def pulse(self, pin: str, width: float, level: int = 1, count: int = 1, period: float = 0.0) -> None:
        """Pulses timed by the device: the edges are sent with the times they should happen at."""
        start = time.monotonic() + 2 * self.remote.typical_delay() + 0.01
        for index in range(int(count)):
            begin = start + index * float(period)
            self.remote.set(pin, bool(level), at=begin)
            self.remote.set(pin, not bool(level), at=begin + float(width))

    def pwm(self, pin: str, frequency: float, duty: float) -> Optional[float]:
        raise InstrumentError(f"{self.remote.device_name} makes no PWM")

    def safe_all(self) -> None:
        """Outputs back to their default (what the device said it starts with)."""
        for name, item in self.remote.outputs().items():
            if item.get("default") is not None:
                try:
                    self.remote.set(name, item["default"])
                except InstrumentError:
                    pass


class RemoteMonitor(MonitorFacet):
    """The latest values of the slow inputs (``device.monitor`` reads them at its rate)."""

    def __init__(self, remote: RemoteFacet) -> None:
        super().__init__()
        self.remote = remote

    def sample(self, pins: list[str], analog: tuple[str, ...] = ()) -> MonitorState:
        digital, values = {}, {}
        for name in list(pins) + list(analog):
            latest = self.remote.latest(name)
            if latest is None or latest.value is None:
                continue
            if name in pins:
                digital[name] = 1 if latest.value else 0
            else:
                values[name] = float(latest.value)
        return MonitorState(time=time.monotonic(), digital=digital, analog=values)

    def start(self, rate: float, pins: list[str], analog: tuple[str, ...] = ()) -> None:
        raise InstrumentError("the remote device sends its values by itself: use remote.receive")


class RemoteAnalogIn(AnalogInFacet):
    """The latest value of a ``scalar`` input (``gpio.read`` with ``analog``)."""

    def __init__(self, remote: RemoteFacet) -> None:
        super().__init__()
        self.remote = remote

    def read(self, pins: list[str]) -> dict[str, float]:
        result = {}
        for pin in pins:
            latest = self.remote.latest(pin)
            if latest is None or latest.value is None:
                raise InstrumentError(f"{self.remote.device_name} has not sent {pin} yet")
            result[pin] = float(latest.value)
        return result


class RemoteInstrument(Instrument):
    """An instrument whose details show the device's inputs, outputs and clock."""

    @property
    def remote(self) -> RemoteFacet:
        return self.require(RemoteFacet)

    @property
    def is_simulated(self) -> bool:
        return self.remote.simulated is not None

    def details(self) -> list[tuple[str, list[tuple[str, str]]]]:
        sections = super().details()
        remote = self.remote
        sections.append(("Inputs", [(name, f"{item['kind']}" + (f" [{item['unit']}]" if item.get("unit") else "")
                                     + (f", {item['rate']:g} /s" if item.get("rate") else "")
                                     + (", sync signal" if item.get("sync") else "")
                                     + (", shared clock" if item.get("clock") == "shared" else "")
                                     + _stamping(remote.latest(name)))
                                    for name, item in remote.inputs().items()] or [("-", "none")]))
        sections.append(("Outputs", [(name, f"{item['kind']}" + (f" [{item['unit']}]" if item.get("unit") else ""))
                                     for name, item in remote.outputs().items()] or [("-", "none")]))
        if remote.commands() or remote.syncs():
            sections.append(("Commands and sync", [(name, "command") for name in remote.commands()]
                             + [(name, f"sync {item['kind']}") for name, item in remote.syncs().items()]))
        state = remote.clock_state()
        if state is None:
            sections.append(("Clock", [("State", "not connected")]))
        else:
            rows = [
                ("Method", {"network": "measured over the network", "signal": "sync signal",
                            "timescale": f"time scale {state.timescale.upper()} (PTP/GPS) of device and computer",
                            "none": "not measured yet"}.get(state.method, state.method)),
                ("Accuracy", f"± {_seconds(state.uncertainty)}"),
            ]
            if not math.isnan(state.check):
                rows.append(("Check over the network", f"{_signed(state.check)} (within ± the network accuracy)"))
            sections.append(("Clock", rows + [
                ("Round trip", _seconds(state.delay)),
                ("Jitter", _seconds(state.jitter)),
                ("Drift", f"{state.drift * 1e6:+.1f} ppm"),
                ("Measurements", f"{state.samples}" + (f", {state.jumps} jump(s)" if state.jumps else "")),
            ]))
        return sections


def _stamping(latest: Optional[Received]) -> str:
    """How the device stamped the last block of an input: its accuracy."""
    if latest is None or latest.samples is None or not latest.method:
        return ""
    text = {"arrival": "stamped on arrival", "bounds": "start and arrival", "latency": "measured latency"}.get(
        latest.method, latest.method)
    if latest.uncertainty is not None:
        text += f" ± {_seconds(latest.uncertainty)}"
    return f" ({text})"


def _signed(value: float) -> str:
    return ("+" if value >= 0 else "-") + _seconds(abs(value))


def _seconds(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return "-"
    from ...core import units

    return units.format_quantity(value, "s", 3)


def make_instrument(server: RemoteServer, name: str, uri: str = "", kind: str = "Remote device",
                    simulated: Any = None) -> RemoteInstrument:
    """The instrument of the connected device ``name``."""
    instrument = RemoteInstrument(name, kind=kind, uri=uri or f"remote:{name}",
                                  status=InstrumentStatus.SIMULATED if simulated is not None else InstrumentStatus.CONNECTED)
    remote = instrument.add_facet(RemoteFacet(server, name, simulated))
    instrument.add_facet(RemoteGpio(remote))
    instrument.add_facet(RemoteMonitor(remote))
    instrument.add_facet(RemoteAnalogIn(remote))
    if simulated is not None:
        instrument.wiring_source = simulated.wiring_source  # its sync output for wires to other simulators
    return instrument


def open_remote(address: str, timeout: float = 15.0) -> RemoteInstrument:
    """``remote:<name>``: the device of that name, connected to the server of this process (it is
    waited for up to ``timeout`` seconds)."""
    from . import servers

    name = address.split(":", 1)[1].strip() if ":" in address else address
    if not name:
        raise InstrumentError("remote:<name> - the name the device gives itself")
    found = servers.find(name)
    if found is not None:
        return make_instrument(found[0], name, uri=f"remote:{name}")
    server = servers.main_server()
    server.wait_for(name, timeout)
    return make_instrument(server, name, uri=f"remote:{name}")
