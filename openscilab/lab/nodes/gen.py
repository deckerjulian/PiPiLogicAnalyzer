# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generator nodes: waveforms, arbitrary points, patterns, protocol transmitters and replays on the
generator outputs of instruments (``GeneratorFacet``)."""

from __future__ import annotations

import functools
import math
import os
from typing import Any, Optional

import numpy as np

from ...core import signals
from ...core import waveform as waves
from ...core.instrument import GeneratorFacet, InstrumentError
from ..engine.runtime import NodeError, NodeRuntime
from .device import DEVICE_INPUT
from .registry import In, Out, Param, node

_OUTPUT_PARAMS = [Param("output", "str", description="the generator output (empty: the first that fits)",
                        suggest="outputs"),
                  Param("duration", "quantity", 0, "s", description="stop after it (0: play until the flow ends)")]
_OUTPUTS = [Out("sync", signals.EVENT, "the output started (its device time)"), Out("done", signals.EVENT)]


class _GeneratorNode(NodeRuntime):
    """Starts waveforms on an output; stops it after 'duration' and when the flow ends."""

    async def setup(self) -> None:
        self._generator: Optional[GeneratorFacet] = None
        self._output: Optional[str] = None

    def generator(self) -> GeneratorFacet:
        instrument = self.ctx.instrument()
        facet = instrument.facet(GeneratorFacet)
        if facet is None:
            raise NodeError(f"{self.node.id}: {instrument.name} has no generator")
        return facet

    def output_for(self, generator: GeneratorFacet, waveform: waves.Waveform) -> str:
        wanted = self.p("output")
        if wanted:
            return str(wanted)
        kind = "pattern" if waveform.is_pattern else "analog"
        for info in generator.outputs():
            if info.kind == kind or (kind == "analog" and info.kind == "square"
                                     and waveform.kind in (waves.SQUARE, waves.PULSE)):
                return info.name
        raise NodeError(f"{self.node.id}: {self.ctx.instrument().name} has no {kind} output")

    def fit(self, waveform: waves.Waveform, output) -> waves.Waveform:
        """``waveform`` as it plays on ``output`` (an ``OutputInfo``); nodes adapt what was not given."""
        return waveform

    async def play(self, waveform: waves.Waveform, done: Any = None) -> None:
        """Plays ``waveform``; after 'duration' (or its own length) it stops and 'done' says ``done``
        (default: the output)."""
        generator = self.generator()
        output = self.output_for(generator, waveform)
        try:
            waveform = self.fit(waveform, generator.output(output))
            started = await self.ctx.device_call(self.ctx.instrument(), generator.start, output, waveform)
        except InstrumentError as error:
            raise NodeError(str(error)) from None
        self._generator, self._output = generator, output
        at = float(started) if isinstance(started, (int, float)) else self.ctx.now()
        self.ctx.emit("sync", signals.Event(times=[at], data=[waveform.describe()]))
        duration = self.q("duration") or 0.0
        if not duration and math.isfinite(waveform.duration) and waveform.repeat > 0:
            duration = waveform.duration
        if duration > 0:
            await self.ctx.sleep(duration)
            self.stop()
            self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[output if done is None else done]))

    def stop(self) -> None:
        if self._generator is not None and self._output is not None:
            try:
                self._generator.stop(self._output)
            except InstrumentError:
                pass
        self._generator = self._output = None

    async def finish(self) -> None:
        self.stop()

    def cleanup(self) -> None:
        self.stop()  # also after an error: the output does not keep playing


def _number(value: Any) -> float:
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return float(value.value)
    if isinstance(value, signals.Event):
        return float(value.data[-1])
    return float(value)


@node("gen.waveform", title="Waveform",
      description="A standard waveform (sine, square, triangle, ramp, pulse with 'duty', DC, noise with "
                  "'amplitude' as its standard deviation) on a generator output. Values at 'frequency' and "
                  "'amplitude' change it while it plays; 'start' starts it (without a wire it starts with the "
                  "flow). On a square-only output (PWM) a square or pulse without 'amplitude' and 'offset' "
                  "swings over the output's whole range.",
      inputs=[DEVICE_INPUT, In("start", signals.ANY), In("frequency", signals.ANY, "Hz"), In("amplitude", signals.ANY, "V")],
      outputs=_OUTPUTS,
      params=_OUTPUT_PARAMS + [Param("kind", "choice", waves.SINE, choices=waves.ANALOG_KINDS[:-1]),
                               Param("frequency", "quantity", "1 kHz", "Hz"), Param("amplitude", "quantity", "1 V", "V"),
                               Param("offset", "quantity", "0 V", "V"), Param("duty", "float", 0.5)],
      icon="wave")
class WaveformNode(_GeneratorNode):
    async def setup(self) -> None:
        await super().setup()
        self.frequency = self.q("frequency")
        self.amplitude = self.q("amplitude")

    def waveform(self) -> waves.Waveform:
        kind = str(self.p("kind", waves.SINE))
        if kind not in waves.ANALOG_KINDS[:-1]:
            raise NodeError(f"kind: {kind!r} is none of {', '.join(waves.ANALOG_KINDS[:-1])}")
        try:
            return waves.Waveform(kind=kind, frequency=self.frequency or 0.0, amplitude=self.amplitude or 0.0,
                                  offset=self.q("offset") or 0.0, duty=float(self.p("duty", 0.5)))
        except waves.WaveformError as error:
            raise NodeError(str(error)) from None

    def fit(self, waveform: waves.Waveform, output) -> waves.Waveform:
        given = self.node.params
        if output.kind == "square" and "amplitude" not in given and "offset" not in given \
                and not self.ctx.wired("amplitude"):
            low, high = output.voltage_range
            return waves.Waveform(kind=waveform.kind, frequency=waveform.frequency, amplitude=(high - low) / 2,
                                  offset=(high + low) / 2, duty=waveform.duty)
        return waveform

    async def run(self) -> None:
        if not self.ctx.wired("start"):
            await self.play(self.waveform())

    async def on_input(self, port: str, value: Any) -> None:
        if port == "frequency":
            self.frequency = _number(value)
        elif port == "amplitude":
            self.amplitude = _number(value)
        if port == "start" or self._generator is not None:
            await self.play(self.waveform())


@node("gen.arbitrary", title="Arbitrary waveform",
      description="Arbitrary points on an analog output: a formula over one pass (x from 0 to 1, numpy "
                  "functions), a CSV file (one column of volts, or time and volts), or the analog signal "
                  "arriving at 'signal'. 'frequency' sets the passes per second; without it a CSV file with "
                  "times and a signal play at their own rate.",
      inputs=[DEVICE_INPUT, In("signal", signals.ANALOG, "points to play")],
      outputs=_OUTPUTS,
      params=_OUTPUT_PARAMS + [Param("formula", "str", "sin(2*pi*x)"), Param("file", "path"),
                               Param("frequency", "quantity", "1 kHz", "Hz"), Param("points", "int", 1024)],
      icon="wave")
class ArbitraryNode(_GeneratorNode):
    def given_frequency(self) -> Optional[float]:
        """'frequency' when it was written: else a CSV file's times or the signal's rate set it."""
        return self.q("frequency") if self.node.params.get("frequency") not in (None, "") else None

    async def run(self) -> None:
        if self.ctx.wired("signal"):
            return
        try:
            if self.p("file"):
                waveform = waves.from_csv(self.ctx.path(str(self.p("file"))), frequency=self.given_frequency())
            else:
                waveform = waves.from_formula(str(self.p("formula")), self.q("frequency") or 1000.0,
                                              int(self.p("points", 1024)))
        except (waves.WaveformError, OSError) as error:
            raise NodeError(str(error)) from None
        await self.play(waveform)

    async def on_input(self, port: str, value: Any) -> None:
        await self.play(waves.from_analog(value, frequency=self.given_frequency()))


@node("gen.pattern", title="Pattern",
      description="A digital pattern on a pattern output: SDL text per pin ('tracks', e.g. {D0: 'l5;h5;'}) "
                  "at 'rate', or the capture arriving at 'capture'. 'repeat': passes (0: until stopped).",
      inputs=[DEVICE_INPUT, In("capture", signals.CAPTURE, "a capture to play as pattern")],
      outputs=_OUTPUTS,
      params=_OUTPUT_PARAMS + [Param("tracks", "dict", description="SDL per pin"),
                               Param("rate", "quantity", "1 MHz", "Hz"), Param("repeat", "int", 0)],
      icon="channels")
class PatternNode(_GeneratorNode):
    async def run(self) -> None:
        if self.ctx.wired("capture"):
            return
        tracks = self.p("tracks") or {}
        if not tracks:
            raise NodeError(f"{self.node.id}: no tracks")
        try:
            waveform = waves.pattern_from_sdl({str(pin): str(text) for pin, text in tracks.items()},
                                              self.q("rate"), repeat=int(self.p("repeat", 0)))
        except waves.WaveformError as error:
            raise NodeError(f"{self.node.id}: {error}") from None
        await self.play(waveform)

    async def on_input(self, port: str, value: Any) -> None:
        waveform = waves.pattern_from_capture(value)
        waveform.repeat = int(self.p("repeat", 0))
        await self.play(waveform)


def _payload(value: Any, default: Any) -> Any:
    """The bytes to send for a value at 'data': its text or bytes, a number as one byte; an event
    without data (a tick) sends the 'data' parameter."""
    if isinstance(value, signals.Event):
        value = value.data[-1] if value.data else None
    if value is None:
        return default
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return bytes([int(value.value) & 0xFF])
    if isinstance(value, (int, np.integer)):
        return bytes([int(value) & 0xFF])
    return value


class _TransmitNode(_GeneratorNode):
    #: ``uart``, ``spi`` or ``i2c``
    protocol = ""

    def tracks(self, data: Any) -> dict[str, np.ndarray]:
        raise NotImplementedError

    def tx_pins(self) -> dict[str, str]:
        """The pins by their role (``tx``; ``sck``, ``mosi``, ``miso``, ``cs``; ``scl``, ``sda``)."""
        raise NotImplementedError

    def tx_settings(self) -> dict[str, Any]:
        return {}

    async def run(self) -> None:
        if not self.ctx.wired("data"):
            await self.send(self.p("data", ""))

    async def on_input(self, port: str, value: Any) -> None:
        await self.send(_payload(value, self.p("data", "")))

    async def send(self, data: Any) -> None:
        instrument = self.ctx.instrument()
        generator = instrument.facet(GeneratorFacet)
        if generator is not None and not self.p("output") and generator.transmits(self.protocol):
            # the device sends it with its own UART, SPI or I²C (and an I²C or SPI target answers)
            try:
                payload = waves._as_bytes(data)
                send = functools.partial(generator.transmit, self.protocol, payload, self.tx_pins(),
                                         **self.tx_settings())
                received = await self.ctx.device_call(instrument, send)
            except (InstrumentError, ValueError, TypeError) as error:
                raise NodeError(str(error)) from None
            self.ctx.emit("sync", signals.Event(times=[self.ctx.now()],
                                                data=[f"{self.protocol.upper()} {len(payload)} byte(s)"]))
            # a device that sends in the background says when it is through (a real one answers then)
            end = getattr(generator, "transmit_end", None)
            if end is not None and end > self.ctx.now():
                await self.ctx.sleep_until(end)
            now = self.ctx.now()
            if self.protocol != "uart":  # (UART only sends)
                self.ctx.emit("received", signals.Event(times=[now], data=[bytes(received)]))
            self.ctx.emit("done", signals.Event(times=[now], data=[len(payload)]))
            return
        try:
            tracks = self.tracks(data)
            count = len(waves._as_bytes(data))
        except (waves.WaveformError, ValueError, TypeError) as error:
            raise NodeError(str(error)) from None
        waveform = waves.pattern(tracks, self.q("rate"), repeat=1)
        await self.play(waveform, done=count)


_TX_PARAMS = _OUTPUT_PARAMS + [Param("data", "str", "Hello", description="text, or bytes as a list"),
                               Param("rate", "quantity", "1 MHz", "Hz",
                                     description="sample rate of the pattern (devices without TX_* hardware)")]
_TX_OUTPUTS = [Out("sync", signals.EVENT, "the transmission started"),
               Out("done", signals.EVENT, "the transmission is through (data: the bytes sent)"),
               Out("received", signals.EVENT, "the bytes read back (SPI: on MISO, I²C: 'read' bytes; "
                                              "with the device's own SPI or I²C)")]


@node("gen.tx_uart", title="Send UART",
      description="Sends the bytes of 'data' (or of each value at 'data') as 8N1 UART frames on 'pin': with "
                  "the device's own UART where it has one (TX_UART), else as a pattern on a pattern output.",
      inputs=[DEVICE_INPUT, In("data", signals.ANY)], outputs=_TX_OUTPUTS,
      params=_TX_PARAMS + [Param("pin", "str", "D0", suggest="pins:DOUT"), Param("baud", "quantity", "115200 Hz", "Hz")],
      icon="arrow-right")
class UartNode(_TransmitNode):
    protocol = "uart"

    def tracks(self, data):
        return waves.uart_tracks(data, self.q("baud"), self.q("rate"), pin=str(self.p("pin", "D0")))

    def tx_pins(self):
        return {"tx": str(self.p("pin", "D0"))}

    def tx_settings(self):
        return {"baud": self.q("baud")}


@node("gen.tx_spi", title="Send SPI",
      description="Sends 'data' as SPI mode 0 (MSB first) on the pins 'pins' (CS, SCK, MOSI); with the "
                  "device's own SPI (TX_SPI) the bytes on 'miso' come back at 'received'.",
      inputs=[DEVICE_INPUT, In("data", signals.ANY)], outputs=_TX_OUTPUTS,
      params=_TX_PARAMS + [Param("pins", "list", ["D0", "D1", "D2"], suggest="pins:DOUT"), Param("miso", "str", "", suggest="pins:DIN"),
                           Param("clock", "quantity", "100 kHz", "Hz")],
      icon="arrow-right")
class SpiNode(_TransmitNode):
    protocol = "spi"

    def _pins(self) -> list[str]:
        pins = [str(pin) for pin in self.p("pins") or ["D0", "D1", "D2"]]
        if len(pins) != 3:
            raise ValueError("pins: CS, SCK and MOSI")
        return pins

    def tracks(self, data):
        return waves.spi_tracks(data, self.q("clock"), self.q("rate"), pins=tuple(self._pins()))

    def tx_pins(self):
        cs, sck, mosi = self._pins()
        pins = {"cs": cs, "sck": sck, "mosi": mosi}
        if self.p("miso"):
            pins["miso"] = str(self.p("miso"))
        return pins

    def tx_settings(self):
        return {"frequency": self.q("clock")}


@node("gen.tx_i2c", title="Send I²C",
      description="Writes 'data' to the I²C 'address' on the pins 'pins' (SCL, SDA) and reads 'read' bytes "
                  "back (devices with their own I²C, TX_I2C; else the write as a pattern).",
      inputs=[DEVICE_INPUT, In("data", signals.ANY)], outputs=_TX_OUTPUTS,
      params=_TX_PARAMS + [Param("pins", "list", ["D0", "D1"], suggest="pins:DOUT"),
                           Param("address", "any", 0x50, description="7 bit: 72 or 0x48"),
                           Param("read", "int", 0), Param("clock", "quantity", "100 kHz", "Hz")],
      icon="arrow-right")
class I2cNode(_TransmitNode):
    protocol = "i2c"

    def address(self) -> int:
        value = self.p("address", 0x50)
        try:
            return int(value, 0) if isinstance(value, str) else int(value)  # ("0x48" too)
        except ValueError:
            raise NodeError(f"address: {value!r} is no number (72, 0x48)") from None

    def _pins(self) -> list[str]:
        pins = [str(pin) for pin in self.p("pins") or ["D0", "D1"]]
        if len(pins) != 2:
            raise ValueError("pins: SCL and SDA")
        return pins

    def tracks(self, data):
        return waves.i2c_tracks(self.address(), data, self.q("clock"), self.q("rate"), pins=tuple(self._pins()))

    def tx_pins(self):
        scl, sda = self._pins()
        return {"scl": scl, "sda": sda}

    def tx_settings(self):
        return {"address": self.address(), "frequency": self.q("clock"), "read": int(self.p("read", 0))}


@node("gen.replay", title="Replay",
      description="Plays a capture: its digital channels on a pattern output under their own names, or only "
                  "the channels of 'pins' under new names (e.g. {CH1: D0}); or one analog channel ('analog') on "
                  "an analog output.",
      inputs=[DEVICE_INPUT, In("capture", signals.CAPTURE)], outputs=_OUTPUTS,
      params=_OUTPUT_PARAMS + [Param("pins", "dict", description="capture channel -> output pin"),
                               Param("analog", "str", description="the analog channel to play", suggest="upstream_channels"),
                               Param("repeat", "int", 1)],
      icon="repeat")
class ReplayNode(_GeneratorNode):
    async def on_input(self, port: str, value: Any) -> None:
        repeat = int(self.p("repeat", 1))
        analog = self.p("analog")
        if analog:
            if analog not in value.analog:
                raise NodeError(f"{self.node.id}: the capture has no analog channel {analog!r}")
            waveform = waves.from_points(value.analog[analog], rate=value.rate, repeat=repeat)
        else:
            mapping = {str(key): str(pin) for key, pin in (self.p("pins") or {}).items()}
            names = list(mapping) or list(value.digital)
            try:
                waveform = waves.pattern_from_capture(value, names)
            except waves.WaveformError as error:
                raise NodeError(f"{self.node.id}: {error}") from None
            waveform.tracks = {mapping.get(name, name): levels for name, levels in waveform.tracks.items()}
            waveform.repeat = repeat
        await self.play(waveform)


@node("gen.output", title="Generator output",
      description="Plays the waveform arriving at 'waveform' (from a *.wave.yaml 'file' without a wire); "
                  "'sync' marks every start, e.g. to arm a capture.",
      inputs=[DEVICE_INPUT, In("waveform", signals.ANY), In("stop", signals.ANY)], outputs=_OUTPUTS,
      params=_OUTPUT_PARAMS + [Param("file", "path", description="a *.wave.yaml or *.sdl file"),
                               Param("pin", "str", "D0", description="a *.sdl file: the pin it plays on",
                                     suggest="pins:DOUT"),
                               Param("rate", "quantity", "1 MHz", "Hz", description="a *.sdl file: its sample rate")],
      icon="play")
class OutputNode(_GeneratorNode):
    async def run(self) -> None:
        if self.ctx.wired("waveform"):
            return
        path = self.p("file")
        if not path:
            raise NodeError(f"{self.node.id}: no waveform (a file or a wire at 'waveform')")
        path = self.ctx.path(str(path))
        if not os.path.exists(path):
            raise NodeError(f"{self.node.id}: no file {path}")
        try:
            waveform = waves.load(path, pin=str(self.p("pin", "D0")), rate=self.q("rate") or 1e6)
        except waves.WaveformError as error:
            raise NodeError(str(error)) from None
        await self.play(waveform)

    async def on_input(self, port: str, value: Any) -> None:
        if port == "stop":
            self.stop()
            self.ctx.emit("done", signals.Event(times=[self.ctx.now()], data=[None]))
            return
        if not isinstance(value, waves.Waveform):
            raise NodeError(f"{self.node.id}: 'waveform' takes waveforms, not {type(value).__name__}")
        await self.play(value)


NODES = [cls.node_spec for cls in (WaveformNode, ArbitraryNode, PatternNode, UartNode, SpiNode, I2cNode,
                                   ReplayNode, OutputNode)]
