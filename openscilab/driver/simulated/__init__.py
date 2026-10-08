# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""openSciLab Sim: simulated instruments that behave like real ones, with their limits.

``sim:<profile>`` opens one (:func:`open_simulated`); the profile (:mod:`.profiles`) says what
the device is, the circuit (:mod:`.circuit`) what its inputs see, the device model
(:mod:`.device`) how it samples them.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from ...core.instrument import Instrument, InstrumentStatus
from .circuit import Circuit
from .device import SimulatedDriver
from .facets import (
    SimulatedAnalogIn,
    SimulatedAnalogOut,
    SimulatedGenerator,
    SimulatedGpio,
    SimulatedMonitor,
    SimulationControl,
)
from . import scenarios
from .profiles import SimAddress, available_profiles, free_address, load_profile, multi_profile

log = logging.getLogger(__name__)


def open_simulated(profile_name: str, clock: Optional[Callable[[], float]] = None, fast: bool = False,
                   seed: int = 1, name: Optional[str] = None, wiring: Optional[list] = None,
                   signals: Optional[dict] = None) -> Instrument:
    """A simulated instrument: ``profile_name`` is a profile (``uno``), a multi device set of
    several boards of it (``pico*2``) or a further instance beside the first (``uno#2``).

    ``clock`` gives the device time in seconds (the engine's clock in a flow); ``fast``: captures
    complete at once (virtual time) instead of taking their real duration. ``signals``: what it
    simulates (:mod:`.scenarios`; default: the test signals of the profile).
    """
    address = SimAddress.parse(profile_name)
    profile = multi_profile(load_profile(address.profile), address.boards)
    circuit = Circuit.from_description(profile.get("circuit"), float(profile.get("logic_level", 3.3)))
    for source in circuit.sources.values():
        if hasattr(source, "seed"):
            source.seed = seed
    circuit.apply_wiring()
    if wiring:
        circuit.apply_wiring(wiring)
    driver = SimulatedDriver(profile, circuit, clock=clock, fast=fast)
    driver.seed = seed
    driver.extra_wiring = list(wiring or [])
    driver.sim_address = address
    if signals:
        scenarios.apply(driver, signals)
    instrument = Instrument.from_driver(driver, name=name or address.title(load_profile(address.profile)["title"]),
                                        uri=str(address))
    instrument.kind = profile["title"]
    instrument.status = InstrumentStatus.SIMULATED
    instrument.simulated_driver = driver  # the device model, also behind the software trigger wrapper
    capabilities = set(profile.get("capabilities", []))
    if "GPIO" in capabilities:
        driver.gpio = instrument.add_facet(SimulatedGpio(driver))
    if "MONITOR" in capabilities:
        instrument.add_facet(SimulatedMonitor(driver))
    if profile.get("analog"):
        instrument.add_facet(SimulatedAnalogIn(driver))
    if "DAC" in capabilities:
        driver.analog_out = instrument.add_facet(SimulatedAnalogOut(driver))
    if profile.get("outputs") or any(item.startswith("TX_") for item in capabilities):
        generator = instrument.add_facet(SimulatedGenerator(driver))
        driver.generator = generator
        if generator.sync_out():
            instrument.trigger_outputs = tuple(instrument.trigger_outputs) + ("SYNC",)
    inputs = profile.get("trigger_inputs") or {}
    if inputs:
        instrument.trigger_inputs = tuple(dict.fromkeys(tuple(instrument.trigger_inputs) + tuple(inputs)))
    instrument.add_facet(SimulationControl(driver))  # (what it simulates: the same here and in a device process)
    return instrument


def _model(instrument) -> Optional[object]:
    """The simulation model of ``instrument`` when it runs in this process (a simulator, or the
    circuit a simulated remote device offers for wires), ``None`` otherwise."""
    return getattr(instrument, "simulated_driver", None) or getattr(instrument, "wiring_source", None)


def _drive(model, net: str, source) -> Callable[[], None]:
    """Drive ``net`` of the local ``model`` with ``source``; returns what gives it back."""
    if hasattr(model, "follow"):
        model.follow(net, source)
        return lambda: model.unfollow(net)
    circuit = model.circuit  # (the circuit of a simulated remote device)
    previous = circuit.sources.get(net)
    circuit.drive(net, source)

    def undo() -> None:
        if circuit.sources.get(net) is source:
            if previous is None:
                circuit.sources.pop(net, None)
            else:
                circuit.sources[net] = previous

    return undo


def connect_nets(source: Instrument, source_net: str, target: Instrument, target_net: str, delay: float = 0.0,
                 label: str = "") -> Callable[[], None]:
    """Wire ``source_net`` of the simulator ``source`` to ``target_net`` of the simulator ``target``
    (``delay`` seconds of cable), wherever they run: in this process the target reads the source's
    circuit directly (also in virtual time), across processes through the net server of the
    source's process (:mod:`.nets`). Returns a function that takes the wire away again; raises
    ``ValueError`` when one of them is no simulator."""
    from .circuit import RemoteSource
    from .nets import NetSource, origin_of, serve

    label = label or f"{source.name} {source_net}"
    source_model, target_model = _model(source), _model(target)
    if source_model is not None and target_model is not None:
        offset = source_model.clock() - target_model.clock()
        high = float(source_model.profile.get("logic_level", 3.3))
        return _drive(target_model, target_net, RemoteSource(source_model.circuit, source_net, offset, delay, high))
    if source.simulation is not None:
        endpoint = source.simulation.endpoint()
    elif source_model is not None:
        endpoint = serve(source_model.circuit, source_model.clock, float(source_model.profile.get("logic_level", 3.3)))
    else:
        raise ValueError(f"{source.name} is no simulator")
    if target.simulation is not None:
        simulation = target.simulation
        simulation.follow(target_net, endpoint, source_net, delay, label)
        return lambda: simulation.unfollow(target_net)
    if target_model is None:
        raise ValueError(f"{target.name} is no simulator")
    offset = origin_of(target_model.clock) - endpoint.origin
    return _drive(target_model, target_net, NetSource(endpoint, source_net, offset, delay, label))


def follow_routes(hub) -> Callable[[], None]:
    """Wire the trigger routes of ``hub`` between simulated instruments - in this process or in
    device processes: the target's input net follows the source's output (with the cable delay).
    Returns a function that stops following."""
    from ...core.hub import EVENT_ADDED, EVENT_ROUTES

    wired: dict[tuple[int, str], tuple] = {}

    def apply(_event=None) -> None:
        if _event is not None and _event.kind not in (EVENT_ROUTES, EVENT_ADDED):
            return
        for route in hub.routes():
            source, target = hub.find(route.source), hub.find(route.target)
            simulations = (getattr(source, "simulation", None), getattr(target, "simulation", None))
            if None in simulations:
                continue
            try:
                output = simulations[0].trigger_output_net(route.output)
                net = simulations[1].trigger_input_net(route.input)
            except Exception:  # noqa: BLE001 - a simulator whose device process ended: no wire
                log.debug("The trigger nets of %s cannot be read", route, exc_info=True)
                continue
            if output is None or net is None:
                continue
            key, token = (id(target), net), (id(source), output, route.delay)
            if wired.get(key) == token:
                continue  # (a wire to another simulator stays when the target simulates something else)
            try:
                connect_nets(source, output, target, net, route.delay, label=f"{route.source} {route.output}")
            except Exception:  # noqa: BLE001 - a route that cannot be wired now is tried at the next change
                log.warning("The trigger route %s → %s cannot be wired", route.source, route.target, exc_info=True)
                continue
            wired[key] = token

    apply()
    return hub.subscribe(apply)


__all__ = ["Circuit", "SimAddress", "SimulatedDriver", "available_profiles", "connect_nets", "follow_routes",
           "free_address", "load_profile", "open_simulated", "scenarios"]


# --------------------------------------------------------- wires and USB
#: what an emulated USB link does by default (``usb`` of a profile; seconds, bytes)
USB_DEFAULTS = {"frame": 0.001, "latency": 0.001, "jitter": 0.0003, "ring": 0}


def wiring_of(driver) -> list[dict]:
    """The wires added to the simulated ``driver`` (beyond those of its profile)."""
    return [dict(wire) for wire in getattr(driver, "extra_wiring", [])]


def set_wiring(driver, wiring: list) -> None:
    """Wire nets of the simulated ``driver`` to each other (``[{from: GP16, to: GP17}, ...]``; also
    ``threshold`` or ``rc``, see :meth:`.circuit.Circuit.apply_wiring`), at once - the signals it
    simulates stay. Raises ``ValueError`` (and changes nothing) for a wire that cannot be."""
    known = set(driver.channel_names()) | set(driver.analog_channel_names())
    gpio = getattr(driver, "gpio", None)
    if gpio is not None:
        known |= {pin.name for pin in gpio.pins()}
    wires = []
    for wire in wiring:
        source, target = str(wire.get("from", "")).strip(), str(wire.get("to", "")).strip()
        if not source or not target or source == target:
            raise ValueError("a wire connects two different pins")
        for net in (source, target):
            if known and net not in known:
                raise ValueError(f"{driver.profile['title']} has no pin {net}")
        wires.append({**{key: value for key, value in wire.items() if key not in ("from", "to")},
                      "from": source, "to": target})
    previous = list(getattr(driver, "extra_wiring", []))
    driver.extra_wiring = wires
    try:
        scenarios.apply(driver, driver.signals)
    except (scenarios.ScenarioError, ValueError) as error:
        driver.extra_wiring = previous
        scenarios.apply(driver, driver.signals)
        raise ValueError(str(error)) from None
    driver.log("wires: " + (", ".join(f"{wire['from']} → {wire['to']}" for wire in wires) or "none"))


def usb_of(driver) -> Optional[dict]:
    """The emulated USB link of the simulated ``driver`` (``None``: it knows the time of its samples)."""
    usb = driver.profile.get("usb")
    return {**USB_DEFAULTS, **usb} if isinstance(usb, dict) else None


def set_usb(driver, usb: Optional[dict]) -> None:
    """Emulate a USB link (``frame``, ``latency``, ``jitter`` in seconds, ``ring`` in bytes): blocks
    arrive late and uneven, the time of the samples is found as with a real device. ``None``: the
    simulator knows the time of its samples."""
    if usb is None:
        driver.profile.pop("usb", None)
        return
    values = {**(usb_of(driver) or USB_DEFAULTS), **usb}
    for key in ("frame", "latency", "jitter"):
        if not 0 <= float(values[key]) <= 1.0:
            raise ValueError(f"USB {key}: 0 to 1 s")
        values[key] = float(values[key])
    values["ring"] = max(int(values.get("ring") or 0), 0)
    driver.profile["usb"] = values
