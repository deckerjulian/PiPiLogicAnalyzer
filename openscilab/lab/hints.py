# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""What a parameter can be: values to offer in the editors and checks of the values a flow has.

The values come from the devices of the flow – what an open instrument or a simulator profile says
it has (capture channels, analog inputs, pins and what each pin can do, generator outputs) – and
from the nodes upstream (the channels of the capture wired to a decoder, the columns of a table).
A parameter says where its values come from with ``ParamSpec.suggest`` (see
:data:`~.nodes.registry.SUGGESTIONS`).

:func:`check` finds values that do not fit: of the wrong type (a word where a number belongs, a
frequency in volts), not one of the choices, a pin the device does not have or that cannot do
what the node needs. These are warnings: a project may run the flow with another device.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..core import units
from .model import DEVICE_NODE, Flow, Problem
from .nodes.registry import NodeSpec, ParamSpec, Registry, RegistryError

log = logging.getLogger(__name__)
#: node types whose ``channels`` parameter names the channels of the capture they send
CAPTURE_TYPES = ("device.capture", "device.stream")
#: nodes that hand a capture on as it came (the channels upstream of them are those of their input)
PASS_THROUGH = ("data.buffer", "dsp.filter", "dsp.resample", "dsp.average")


@dataclass
class DeviceHints:
    """What a device has, as far as the application knows it without asking the device."""

    address: str
    title: str = ""
    #: capture channels (digital first, then analog)
    channels: list[str] = field(default_factory=list)
    analog: list[str] = field(default_factory=list)
    #: pin name -> what it can do (``DIN``, ``DOUT``, ``PWM``, ``ADC``, ``DAC``, ...)
    pins: dict[str, frozenset] = field(default_factory=dict)
    #: pins that cannot be used, with the reason
    reserved: dict[str, str] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    #: what a remote device describes: name -> kind
    remote_inputs: dict[str, str] = field(default_factory=dict)
    remote_outputs: dict[str, str] = field(default_factory=dict)
    remote_commands: list[str] = field(default_factory=list)
    remote_syncs: list[str] = field(default_factory=list)

    def pins_that(self, capability: str = "") -> list[str]:
        return [name for name, can in self.pins.items() if not capability or capability in can]


def from_profile(address: str, profile: dict) -> DeviceHints:
    hints = DeviceHints(address, str(profile.get("title", address)),
                        [str(name) for name in profile.get("digital", [])] + [str(name) for name in profile.get("analog", [])],
                        [str(name) for name in profile.get("analog", [])])
    for pin in profile.get("pins") or []:
        name = str(pin.get("name", ""))
        if name:
            hints.pins[name] = frozenset(str(item) for item in pin.get("caps", []))
            if pin.get("reserved"):
                hints.reserved[name] = str(pin["reserved"])
    hints.outputs = [str(output.get("name")) for output in profile.get("outputs") or [] if output.get("name")]
    return hints


def from_remote(address: str, description: dict, title: str = "") -> DeviceHints:
    """The hints of a remote device from the description it sends."""
    hints = DeviceHints(address, title or str(description.get("name") or address))
    hints.remote_inputs = {item["name"]: item["kind"] for item in description.get("inputs") or []}
    hints.remote_outputs = {item["name"]: item["kind"] for item in description.get("outputs") or []}
    hints.remote_commands = [item["name"] for item in description.get("commands") or []]
    hints.remote_syncs = [item["name"] for item in description.get("sync") or []]
    hints.pins = {name: frozenset({"DOUT"}) for name, kind in hints.remote_outputs.items() if kind == "bool"}
    hints.pins.update({name: frozenset({"DIN"}) for name, kind in hints.remote_inputs.items() if kind == "bool"})
    hints.analog = [name for name, kind in hints.remote_inputs.items() if kind == "scalar"]
    return hints


def from_instrument(instrument) -> DeviceHints:
    """The hints of an open instrument (what its facets tell; a part that cannot answer is left out)."""
    from ..driver.remote.instrument import RemoteFacet

    remote = instrument.facet(RemoteFacet)
    if remote is not None:
        return from_remote(instrument.uri, remote.description, instrument.name)
    hints = DeviceHints(instrument.uri, instrument.name)
    try:
        pins = instrument.pins() if instrument.gpio is not None else []
    except Exception:  # noqa: BLE001 - a device that cannot answer
        pins = []
    for pin in pins:
        hints.pins[pin.name] = frozenset(pin.capabilities)
        if pin.reserved:
            hints.reserved[pin.name] = pin.reserved
    capture = instrument.capture
    if capture is not None:
        try:
            names = getattr(getattr(instrument, "simulated_driver", None), "channel_names", None)
            digital = list(names()) if callable(names) else [pin.name for pin in instrument.pins() if pin.channel is not None]
            driver = capture.driver
            analog = list(driver.analog_channel_names()) if driver.analog_channel_count else []
            hints.channels, hints.analog = digital + analog, analog
        except Exception:  # a device that cannot answer: no channels to offer
            log.debug("%s does not tell its channels", instrument.name, exc_info=True)
    generator = instrument.generator
    if generator is not None:
        try:
            hints.outputs = [output.name for output in generator.outputs()]
        except Exception:
            log.debug("%s does not tell its outputs", instrument.name, exc_info=True)
    return hints


def device_hints(address: str, instruments: Iterable = ()) -> Optional[DeviceHints]:
    """The hints of the device at ``address``: an open instrument of that address, else a simulator
    profile; ``None`` for a device that is not open (a board on a port: its pins come when it is)."""
    address = (address or "").strip()
    if not address:
        return None
    for instrument in instruments:
        if instrument.uri == address:
            return from_instrument(instrument)
    from ..driver.simulated import profiles

    kind, _, rest = address.partition(":")
    if kind == "remote":
        from ..driver.remote import servers

        found = servers.find(rest.strip())
        return from_remote(address, found[1].description) if found is not None else None
    if kind == "remote-sim":
        from ..driver.remote import simulated

        try:
            name, _instance, options = simulated.parse(address)
        except ValueError:
            return None
        return from_remote(address, simulated.describe(name, options), f"Simulation: remote {name}")
    try:
        if kind == "sim":
            parsed = profiles.SimAddress.parse(address)
            profile = profiles.multi_profile(profiles.load_profile(parsed.profile), parsed.boards)
        elif kind == "arduino-sim":
            profile = profiles.load_profile(rest or "uno")
        elif kind == "rigol-sim":
            profile = profiles.load_profile("dho924s")
        else:
            return None
    except (OSError, ValueError, KeyError):
        return None
    return from_profile(address, profile)


def flow_devices(flow: Flow, project_devices: Optional[dict] = None, instruments: Iterable = ()) -> dict[str, DeviceHints]:
    """Hints of the device nodes of ``flow`` (an empty address: the project's device of that name)."""
    instruments = list(instruments)
    found = {}
    for name, address in flow.devices.items():
        hints = device_hints(address or (project_devices or {}).get(name, ""), instruments)
        if hints is not None:
            found[name] = hints
    return found


# ---------------------------------------------------------------- upstream
def _source_of(flow: Flow, node_id: str, port: str = "in", seen: Optional[set] = None):
    """The node (and its output) that sends into ``node_id.port``, through pass-through nodes."""
    seen = seen or set()
    for edge in flow.edges_into(node_id, port):
        source = flow.nodes.get(edge.source.node)
        if source is None or source.id in seen:
            continue
        if source.type in PASS_THROUGH:
            seen.add(source.id)
            found = _source_of(flow, source.id, "in", seen)
            if found is not None:
                return found
        return source, edge.source.port
    return None


def upstream_channels(flow: Flow, node_id: str) -> list[str]:
    """The channels of the capture wired (directly or through filters) into ``node_id``."""
    found = _source_of(flow, node_id)
    if found is None:
        return []
    source, port = found
    if source.type in CAPTURE_TYPES:
        channels = source.params.get("channels") or []
        if port != "capture":
            return [port] if port in [str(channel) for channel in channels] else []
        return [str(channel) for channel in channels]
    if source.type == "convert.channel" and source.params.get("channel"):
        return [str(source.params["channel"])]
    return []


def upstream_columns(flow: Flow, node_id: str) -> list[str]:
    """The columns of the table wired into ``node_id``."""
    found = _source_of(flow, node_id)
    if found is None:
        return []
    source, _port = found
    if source.type == "data.table":
        return [str(column) for column in source.params.get("columns") or []]
    return []


# ------------------------------------------------------------- suggestions
def suggestions(flow: Flow, node_id: str, param: ParamSpec, devices: dict[str, DeviceHints]) -> Optional[list[str]]:
    """Values to offer for ``param`` of ``node_id``; ``None`` when nothing is known (no device that
    tells, nothing wired upstream) – the editor then takes any text."""
    source, _, capability = param.suggest.partition(":")
    if not source:
        return None
    if source == "upstream_channels":
        return upstream_channels(flow, node_id) or None
    if source == "upstream_columns":
        return upstream_columns(flow, node_id) or None
    device = flow.device_of(node_id) if node_id in flow.nodes else None
    if device is None and len(flow.devices) == 1:
        device = next(iter(flow.devices))  # not wired yet: the only device is the one it will use
    hints = devices.get(device) if device else None
    if hints is None:
        return None
    if source == "channels":
        return list(hints.channels) or None
    if source == "analog":
        return list(hints.analog) or None
    if source == "outputs":
        return list(hints.outputs) or None
    if source == "pins":
        if not hints.pins:
            return None
        return [name for name in hints.pins_that(capability) if name not in hints.reserved]
    remote = _remote_values(hints, source)
    return remote or None


def _remote_values(hints: DeviceHints, source: str) -> list[str]:
    return {"remote_inputs": list(hints.remote_inputs), "remote_outputs": list(hints.remote_outputs),
            "remote_commands": list(hints.remote_commands), "remote_syncs": list(hints.remote_syncs)}.get(source, [])


def node_suggestions(flow: Flow, node_id: str, spec: NodeSpec, devices: dict[str, DeviceHints]) -> dict[str, list[str]]:
    """``param name -> values`` of the parameters of ``node_id`` that have something to offer."""
    found = {}
    for param in spec.params:
        values = suggestions(flow, node_id, param, devices)
        if values is not None:
            found[param.name] = values
    return found


# ------------------------------------------------------------------ checks
def value_problem(param: ParamSpec, value) -> str:
    """What is wrong with ``value`` for ``param`` by its kind (``""``: nothing)."""
    if value is None:
        return ""
    kind = param.kind
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        return f"{param.name}: {value!r} is not a whole number"
    if kind == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return f"{param.name}: {value!r} is not a number"
    if kind == "bool" and not isinstance(value, bool):
        return f"{param.name}: {value!r} is not true or false"
    if kind == "quantity":
        if isinstance(value, bool):
            return f"{param.name}: {value!r} is not a quantity"
        try:
            units.parse(value, param.unit)
        except (ValueError, TypeError, units.UnitError) as error:
            return f"{param.name}: {error}"
    if kind == "choice" and param.choices and value not in param.choices \
            and str(value) not in [str(choice) for choice in param.choices]:
        return f"{param.name}: {value!r} is not one of {', '.join(str(choice) for choice in param.choices)}"
    if kind == "list" and not isinstance(value, (list, tuple)):
        return f"{param.name}: {value!r} is not a list (e.g. [D0, D1])"
    if kind == "dict" and not isinstance(value, dict):
        return f"{param.name}: {value!r} is not a mapping (e.g. {{key: value}})"
    if kind == "dict" and param.keys:
        unknown = [str(key) for key in value if str(key) not in param.keys]
        if unknown:
            return f"{param.name}: no {', '.join(unknown)} (keys: {', '.join(param.keys)})"
    return ""


def _values(param: ParamSpec, value) -> list[str]:
    """The single values in a parameter value (a list's items, a mapping's values)."""
    if isinstance(value, dict):
        return [str(item) for item in value.values() if item not in (None, "")]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)] if value not in (None, "") else []


def device_problem(flow: Flow, node_id: str, param: ParamSpec, value, devices: dict[str, DeviceHints]) -> str:
    """A value the device (or the node upstream) does not have; ``""`` when it fits or nothing is known."""
    source, _, capability = param.suggest.partition(":")
    if not source or value in (None, "", [], {}):
        return ""
    if source in ("upstream_channels", "upstream_columns"):
        known = upstream_channels(flow, node_id) if source == "upstream_channels" else upstream_columns(flow, node_id)
        what = "channel" if source == "upstream_channels" else "column"
        missing = [item for item in _values(param, value) if known and item not in known
                   and not (source == "upstream_columns" and all(part.strip() in known for part in item.split(",")))]
        return f"{param.name}: the input has no {what} {', '.join(missing)} ({', '.join(known)})" if missing else ""
    device = flow.device_of(node_id)
    hints = devices.get(device) if device else None
    if hints is None:
        return ""
    if source == "pins":
        if not hints.pins:
            return ""
        for item in _values(param, value):
            if item not in hints.pins:
                return f"{param.name}: {hints.title} has no pin {item}"
            if item in hints.reserved:
                return f"{param.name}: {item} of {hints.title} is reserved ({hints.reserved[item]})"
            if capability and capability not in hints.pins[item]:
                able = hints.pins_that(capability)
                return (f"{param.name}: {item} of {hints.title} cannot do {capability}"
                        + (f" (pins that can: {', '.join(able)})" if able else ""))
        return ""
    known = {"channels": hints.channels, "analog": hints.analog, "outputs": hints.outputs}.get(source) \
        or _remote_values(hints, source)
    missing = [item for item in _values(param, value) if known and item not in known]
    if missing:
        what = {"channels": "channel", "analog": "analog input", "outputs": "output", "remote_inputs": "input",
                "remote_outputs": "output", "remote_commands": "command", "remote_syncs": "sync signal"}[source]
        return f"{param.name}: {hints.title} has no {what} {', '.join(missing)} ({', '.join(known)})"
    return ""


def check(flow: Flow, registry: Optional[Registry] = None, devices: Optional[dict[str, DeviceHints]] = None) -> list[Problem]:
    """Warnings for parameter values that do not fit their kind, their device or their input."""
    devices = devices or {}
    problems = []
    for node_id, node in flow.nodes.items():
        if node.type == DEVICE_NODE:
            continue
        try:
            spec = flow.spec(node, registry)
        except RegistryError:
            continue  # validate() reports it
        for param in spec.params:
            if param.name not in node.params:
                continue
            value = node.params[param.name]
            text = value_problem(param, value) or device_problem(flow, node_id, param, value, devices)
            if text:
                problems.append(Problem("warning", text, node_id))
    return problems
