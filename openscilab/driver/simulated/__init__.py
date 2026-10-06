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
)
from . import scenarios
from .profiles import SimAddress, available_profiles, free_address, load_profile, multi_profile


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
    return instrument


def _net_of_output(instrument: Instrument, output: str) -> Optional[str]:
    driver = getattr(instrument, "simulated_driver", None)
    if driver is None:
        return None
    if output == "SYNC":
        from ...core.instrument import GeneratorFacet

        generator = instrument.facet(GeneratorFacet)
        return generator.sync_net() if generator is not None else None
    return {"TRIG OUT": driver.profile.get("trigger_out_net")}.get(output)


def _net_of_input(instrument: Instrument, name: str) -> Optional[str]:
    driver = getattr(instrument, "simulated_driver", None)
    if driver is None:
        return None
    return (driver.profile.get("trigger_inputs") or {}).get(name)


def follow_routes(hub) -> Callable[[], None]:
    """Wire the trigger routes of ``hub`` between simulated instruments: the target's input net
    follows the source's output (with the cable delay). Returns a function that stops following."""
    from ...core.hub import EVENT_ADDED, EVENT_ROUTES
    from .circuit import RemoteSource

    wired: dict[tuple[int, str], object] = {}

    def apply(_event=None) -> None:
        if _event is not None and _event.kind not in (EVENT_ROUTES, EVENT_ADDED):
            return
        for route in hub.routes():
            source, target = hub.find(route.source), hub.find(route.target)
            if source is None or target is None:
                continue
            output, net = _net_of_output(source, route.output), _net_of_input(target, route.input)
            if output is None or net is None:
                continue
            source_driver, target_driver = source.simulated_driver, target.simulated_driver
            key = (id(target_driver), net)
            current = target_driver.circuit.sources.get(net)
            if key in wired and current is wired[key]:
                continue
            offset = source_driver.clock() - target_driver.clock()
            remote = RemoteSource(source_driver.circuit, output, offset, route.delay,
                                  high=float(source_driver.profile.get("logic_level", 3.3)))
            target_driver.circuit.drive(net, remote)
            wired[key] = remote
            target_driver.log(f"{net}: wired from {route.source} {route.output}")

    apply()
    return hub.subscribe(apply)


__all__ = ["Circuit", "SimAddress", "SimulatedDriver", "available_profiles", "follow_routes", "free_address",
           "load_profile", "open_simulated", "scenarios"]


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
