# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""GPIO nodes: write, pulse, PWM, read, DAC and the monitor of instruments with those facets."""

from __future__ import annotations

from typing import Any

from ...core import signals, units
from ...core.instrument import (
    MODE_OUTPUT,
    AnalogOutFacet,
    CaptureFacet,
    GpioFacet,
    InstrumentError,
    MonitorFacet,
)
from ..engine.runtime import NodeError, NodeRuntime
from .device import DEVICE_INPUT
from .registry import In, Out, Param, node


def number(value: Any) -> float:
    """A value arriving at an input as a number (scalars, truth values, events with data, plain numbers)."""
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return float(value.value)
    if isinstance(value, signals.Event):
        data = [item for item in value.data if item is not None]
        if not data:
            raise NodeError("an event without a value")
        return float(data[-1])
    try:
        return float(value)
    except (TypeError, ValueError):
        raise NodeError(f"not a number: {value!r}") from None


def level_of(value: Any) -> int:
    """A logic level: 1, 0, true, false, high, low (a number: 0.5 or more is 1)."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("high", "on", "true", "h"):
            return 1
        if text in ("low", "off", "false", "l"):
            return 0
    try:
        return 1 if float(value) >= 0.5 else 0
    except (TypeError, ValueError):
        raise NodeError(f"level: {value!r} is no level (1, 0, high, low)") from None


def pin_names(instrument) -> list[str]:
    """The pins of ``instrument`` and its analog inputs, by name."""
    names = [info.name for info in instrument.pins()]
    capture = instrument.facet(CaptureFacet)
    driver = getattr(capture, "driver", None)
    if driver is not None and getattr(driver, "analog_channel_count", 0):
        names += [name for name in driver.analog_channel_names() if name not in names]
    return names


def known_pin(instrument, name: str) -> str:
    """``name`` when ``instrument`` has that pin; else an error that names the pins it has."""
    names = pin_names(instrument)
    if not names or name in names:
        return name
    same = [item for item in names if item.lower() == name.lower()]
    hint = f" - {same[0]}?" if same else f" (pins: {', '.join(names[:48])}{', ...' if len(names) > 48 else ''})"
    raise NodeError(f"{instrument.name} has no pin {name!r}{hint}")


class _PinNode(NodeRuntime):
    def facet(self, cls):
        instrument = self.ctx.instrument()
        facet = instrument.facet(cls)
        if facet is None:
            raise NodeError(f"{instrument.name} has no {cls.title}")
        return facet

    def pin(self) -> str:
        pin = self.p("pin")
        if not pin:
            raise NodeError("no pin")
        return known_pin(self.ctx.instrument(), str(pin))

    async def act(self, action) -> Any:
        """A call of the device (a real one answers in a thread, see ``NodeContext.device_call``)."""
        try:
            return await self.ctx.device_call(self.ctx.instrument(), action)
        except InstrumentError as error:
            raise NodeError(str(error)) from None

    def done(self, value: float) -> None:
        self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[value]))


@node("gpio.write", title="Write pin",
      description="Drives an output pin to the value arriving at 'value' (≥ 0.5 is 1). Without a wire "
                  "it sets 'level' once when the flow starts.",
      inputs=[DEVICE_INPUT, In("value", signals.ANY, "0 or 1")],
      outputs=[Out("done", signals.EVENT)],
      params=[Param("pin", "str", required=True, suggest="pins:DOUT"),
              Param("level", "any", 1, description="1, 0, high or low")],
      icon="power")
class WriteNode(_PinNode):
    async def run(self) -> None:
        if not self.ctx.wired("value"):
            await self.write(level_of(self.p("level", 1)))

    async def on_input(self, port: str, value: Any) -> None:
        await self.write(1 if number(value) >= 0.5 else 0)

    async def write(self, level: int) -> None:
        gpio = self.facet(GpioFacet)
        pin = self.pin()
        if gpio.mode(pin) != MODE_OUTPUT:
            await self.act(lambda: gpio.set_mode(pin, MODE_OUTPUT))
        await self.act(lambda: gpio.write(pin, level))
        self.done(level)


@node("gpio.pulse", title="Pulse",
      description="'count' pulses of 'width', one every 'period', on a pin for every value at "
                  "'trigger' (once at the start without a wire). The device times the pulses exactly.",
      inputs=[DEVICE_INPUT, In("trigger", signals.ANY)],
      outputs=[Out("done", signals.EVENT)],
      params=[Param("pin", "str", required=True, suggest="pins:DOUT"),
              Param("width", "quantity", "10 ms", "s"), Param("level", "any", 1, description="1 or high, 0 or low"),
              Param("count", "int", 1), Param("period", "quantity", "0 s", "s")],
      icon="play")
class PulseNode(_PinNode):
    async def run(self) -> None:
        if not self.ctx.wired("trigger"):
            await self.pulse()

    async def on_input(self, port: str, value: Any) -> None:
        await self.pulse()

    async def pulse(self) -> None:
        gpio = self.facet(GpioFacet)
        width = self.q("width")
        if not width or width <= 0:
            raise NodeError("width must be positive")
        count = int(self.p("count", 1))
        period = self.q("period") or 0.0
        if count < 1:
            raise NodeError("count: at least 1 pulse")
        if count > 1 and period <= width:
            raise NodeError("several pulses need a period longer than the width")
        level = level_of(self.p("level", 1))
        if count == 1:
            await self.act(lambda: gpio.pulse(self.pin(), width, level))
        else:
            await self.act(lambda: gpio.pulse(self.pin(), width, level, count, period))
        await self.ctx.sleep((count - 1) * period + width)
        self.done(width)


@node("gpio.pwm", title="PWM",
      description="PWM on a pin: 'freq' and the duty cycle (0..1) from 'duty' or the parameter; "
                  "a duty of 0 stops it.",
      inputs=[DEVICE_INPUT, In("duty", signals.ANY, "duty cycle 0..1")],
      outputs=[Out("done", signals.EVENT)],
      params=[Param("pin", "str", required=True, suggest="pins:PWM"),
              Param("freq", "quantity", "1 kHz", "Hz"), Param("duty", "quantity", 0.5, description="0..1 or 50 %")],
      icon="wave")
class PwmNode(_PinNode):
    async def run(self) -> None:
        if not self.ctx.wired("duty"):
            try:
                duty = units.parse(self.p("duty", 0.5))
            except units.UnitError as error:
                raise NodeError(f"duty: {error}") from None
            await self.pwm(duty)

    async def on_input(self, port: str, value: Any) -> None:
        await self.pwm(number(value))

    async def pwm(self, duty: float) -> None:
        if not 0.0 <= duty <= 1.0:
            raise NodeError(f"the duty cycle is 0..1 (0 % to 100 %), not {duty:g}")
        gpio = self.facet(GpioFacet)
        frequency = self.q("freq")
        if not frequency or frequency <= 0:
            raise NodeError("freq must be positive")
        await self.act(lambda: gpio.pwm(self.pin(), frequency, duty))
        self.done(duty)


@node("gpio.read", title="Read pin",
      description="Reads a pin for every value at 'trigger' (once at the start without a wire): the "
                  "level of a digital pin, the voltage of an analog input ('analog').",
      inputs=[DEVICE_INPUT, In("trigger", signals.ANY)],
      outputs=[Out("value", signals.SCALAR)],
      params=[Param("pin", "str", required=True, suggest="pins:DIN"),
              Param("analog", "bool", False)],
      icon="target")
class ReadNode(_PinNode):
    async def run(self) -> None:
        if not self.ctx.wired("trigger"):
            await self.read()

    async def on_input(self, port: str, value: Any) -> None:
        await self.read()

    async def read(self) -> None:
        pin = self.pin()
        if self.p("analog", False):
            from ...core.instrument import AnalogInFacet

            facet = self.facet(AnalogInFacet)
            values: dict = {}
            await self.act(lambda: values.update(facet.read([pin])))
            self.ctx.emit("value", signals.Scalar(name=pin, unit="V", value=values[pin], at=self.ctx.now()))
            return
        gpio = self.facet(GpioFacet)
        levels: list = []
        await self.act(lambda: levels.append(gpio.read(pin)))
        self.ctx.emit("value", signals.Scalar(name=pin, value=float(levels[0]), at=self.ctx.now()))


@node("gpio.dac", title="Voltage",
      description="Sets an analog output to the voltage arriving at 'volts' (or the parameter once).",
      inputs=[DEVICE_INPUT, In("volts", signals.ANY)],
      outputs=[Out("done", signals.EVENT)],
      params=[Param("pin", "str", required=True, suggest="pins:DAC"),
              Param("volts", "quantity", "0 V", "V")],
      icon="sliders")
class DacNode(_PinNode):
    async def run(self) -> None:
        if not self.ctx.wired("volts"):
            await self.set(self.q("volts") or 0.0)

    async def on_input(self, port: str, value: Any) -> None:
        await self.set(number(value))

    async def set(self, volts: float) -> None:
        facet = self.facet(AnalogOutFacet)
        pin = self.pin()
        low, high = facet.voltage_range(pin)
        if not low - 1e-9 <= volts <= high + 1e-9:
            self.ctx.log(f"{units.format_quantity(volts, 'V')} is outside {units.format_quantity(low, 'V')} to "
                         f"{units.format_quantity(high, 'V')}: limited to the range")
        result = await self.act(lambda: facet.set_voltage(pin, volts))
        # what the output gives (limited to its range, in its steps), where the device says it
        self.done(float(result) if isinstance(result, (int, float)) else min(max(volts, low), high))


def _monitor_ports(params: dict):
    names = [str(name) for name in (params.get("pins") or [])] + [str(name) for name in (params.get("analog") or [])]
    return [], [Out(name, signals.SCALAR, f"pin {name}") for name in names]


@node("device.monitor", title="Monitor",
      description="Reads pins periodically at 'rate' for 'duration' (until the flow ends with 0): a "
                  "value per pin and report (1 s at 10 Hz: 10 reports, the first at once).",
      inputs=[DEVICE_INPUT],
      outputs=[Out("state", signals.EVENT, "every report")],
      params=[Param("pins", "list", description="digital pins", suggest="pins:DIN"),
              Param("analog", "list", description="analog inputs", suggest="analog"), Param("rate", "quantity", "10 Hz", "Hz"),
              Param("duration", "quantity", "1 s", "s")],
      ports=_monitor_ports, icon="eye")
class MonitorNode(_PinNode):
    async def run(self) -> None:
        monitor = self.facet(MonitorFacet)
        rate = self.q("rate")
        if not rate or rate <= 0:
            raise NodeError(f"{self.node.id}: rate must be positive")
        device = self.ctx.instrument()
        pins = [known_pin(device, str(name)) for name in self.p("pins") or []]
        analog = tuple(known_pin(device, str(name)) for name in self.p("analog") or [])
        duration = self.q("duration") or 0.0
        sample = getattr(monitor, "sample", None)
        if sample is None:
            raise NodeError(f"{self.node.id}: the monitor of {self.ctx.instrument().name} reports only while it runs")
        index = 0
        start = self.ctx.now()
        while duration <= 0 or index / rate < duration - 1e-12:
            state = None
            try:
                state = await self.ctx.device_call(device, sample, pins, analog)
            except InstrumentError as error:
                raise NodeError(str(error)) from None
            now = self.ctx.now()
            for name, level in state.digital.items():
                if name in self.ctx.output_ports:
                    self.ctx.emit(name, signals.Scalar(name=name, value=float(level), at=now))
            for name, volts in state.analog.items():
                if name in self.ctx.output_ports:
                    self.ctx.emit(name, signals.Scalar(name=name, unit="V", value=volts, at=now))
            self.ctx.emit("state", signals.Event(times=[now], data=[{**state.digital, **state.analog}]))
            index += 1
            # the n-th report at start + n / rate: reading the pins does not stretch the period
            await self.ctx.sleep_until(start + index / rate)


NODES = [spec.node_spec for spec in (WriteNode, PulseNode, PwmNode, ReadNode, DacNode, MonitorNode)]
