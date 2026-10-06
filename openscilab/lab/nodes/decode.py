# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Decoder nodes: ``decode.<id>`` for every sigrok decoder found (``uart``, ``i2c``, ``spi``, ...).

A decoder node takes a capture and sends its annotations: ``events`` (an event per annotation,
data ``{row, value, values, start, end}``), ``table`` (the same as rows) and ``text`` (the values of
one row joined, e.g. the bytes of a UART; ``[0A]`` for unprintable bytes becomes the byte).
``channels`` maps the decoder's channels to channels of the capture (``{rx: D9}``); the decoder's
options are parameters of the node. Decoders that work on another decoder's output get it stacked
below them.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ...core import signals
from ...sigrok.engine import DecoderInfo, DecoderRegistry, OptionType
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, NodeSpec, Out, Param, ParamSpec

_HEX_RE = re.compile(r"\[([0-9A-Fa-f]{2})\]")
_registry: Optional[DecoderRegistry] = None


def decoder_registry() -> DecoderRegistry:
    global _registry
    if _registry is None:
        _registry = DecoderRegistry()
    _registry.load()  # once
    return _registry


def use_registry(registry: DecoderRegistry) -> None:
    """The decoders of the application (with its decoder folders) instead of a registry of
    their own: flows decode with the decoders the data views use, and they are loaded once."""
    global _registry
    _registry = registry


def text_of(values: list[str]) -> str:
    """Values of a data row joined; ``[0A]`` (how sigrok shows bytes it cannot print) becomes the byte."""
    return "".join(_HEX_RE.sub(lambda match: chr(int(match.group(1), 16)), value) for value in values)


class DecodeNode(NodeRuntime):
    """Runs the decoder ``spec.decoder_id`` on every capture that arrives."""

    async def on_input(self, port: str, value: Any) -> None:
        if not isinstance(value, signals.Capture):
            raise NodeError(f"{self.node.id}: expects a capture")
        info: DecoderInfo = self.spec.implementation.decoder_info
        options = {}
        for option in info.options:
            if option.id in self.params and self.params[option.id] is not None:
                options[option.id] = self.params[option.id]
        channels = dict(self.p("channels") or {})
        if not channels:
            # Channels of the capture named like the decoder's channels (rx, SDA, ...).
            names = {name.lower(): name for name in value.digital}
            channels = {channel.id: names[channel.id.lower()] for channel in info.channels
                        if channel.id.lower() in names}
        records = await self.ctx.external(self._decode, value, info.id, channels, options)
        rows = self.p("rows")
        if rows:
            records = [record for record in records if record.row in rows]
        offset = value.start + value.trigger / value.rate
        events = signals.Event(
            name=self.node.id,
            times=[offset + record.start_time for record in records],
            data=[{"row": record.row, "value": record.value, "values": list(record.values), "start": offset + record.start_time,
                   "end": offset + record.end_time} for record in records],
        )
        table = signals.Table(name=self.node.id, columns={
            "time": [offset + record.start_time for record in records],
            "row": [record.row for record in records],
            "value": [record.value for record in records],
        })
        if not channels:
            self.ctx.log(f"{info.id}: no channel of the capture is named like the decoder's "
                         f"({', '.join(channel.id for channel in info.channels)}): set 'channels'")
        text = self.text(info, records)
        self.ctx.emit("events", events)
        self.ctx.emit("table", table)
        self.ctx.emit("text", text)

    @staticmethod
    def _rows(info: DecoderInfo) -> list[str]:
        return [row[1] for row in info.annotation_rows]

    def text(self, info: DecoderInfo, records: list) -> str:
        """The 'text_row' joined; by default the first data row that has annotations (an SPI with only
        MOSI wired: its MOSI data). Of a row that also holds starts, addresses and acknowledges (I²C)
        only the data, in its shortest form."""
        wanted = self.p("text_row")
        rows = [wanted] if wanted else [row for row in self._rows(info) if "data" in row.lower()]
        chosen = next(([record for record in records if record.row == row] for row in rows
                       if any(record.row == row for record in records)), [])

        def kind(record) -> str:
            return str(info.annotations[record.type_id][0]) if 0 <= record.type_id < len(info.annotations) else ""

        data = [record for record in chosen if "data" in kind(record).lower()]
        if data and len(data) < len(chosen):
            return text_of([record.values[-1] if record.values else record.value for record in data])
        return text_of([record.value for record in chosen if not record.value.lower().endswith(" bit")])

    @staticmethod
    def _decode(capture: signals.Capture, decoder: str, channels: dict, options: dict) -> list:
        from ... import api
        from ...sigrok import worker

        session = capture.to_session()
        # (in the decoder process: a flow that records and decodes at once must not stall its stream)
        return api.Capture(session).decode(decoder, channels=channels, options=options,
                                           registry=decoder_registry(), run=worker.run)


def _option_param(option) -> ParamSpec:
    if option.values:
        return Param(option.id, "choice", option.default, choices=option.values, description=option.caption)
    kind = {OptionType.INTEGER: "int", OptionType.DOUBLE: "float", OptionType.BOOLEAN: "bool"}.get(
        option.option_type, "str")
    return Param(option.id, kind, option.default, description=option.caption)


def spec_for(info: DecoderInfo) -> NodeSpec:
    implementation = type(f"Decode_{info.id}", (DecodeNode,), {"decoder_info": info})
    channels = ", ".join(f"{channel.id}{'' if channel.required else '?'}" for channel in info.channels)
    params = [Param("channels", "dict", description=f"decoder channel: capture channel ({channels or 'stacked'})",
                    suggest="upstream_channels", keys=[channel.id for channel in info.channels])]
    params += [_option_param(option) for option in info.options]
    params += [Param("rows", "list", description="only these annotation rows"),
               Param("text_row", "str", description="row joined into 'text' (default: the first data row with "
                                                     "annotations)")]
    return NodeSpec(
        type=f"decode.{info.id}",
        implementation=implementation,
        title=info.name,
        description=f"{info.longname}: {info.desc}".strip(": "),
        inputs=[In("in", signals.CAPTURE, optional=False)],
        outputs=[Out("events", signals.EVENT), Out("table", signals.TABLE), Out("text", signals.ANY)],
        params=params,
        icon="bus",
        source=info.path,
    )


def nodes() -> list[NodeSpec]:
    return [spec_for(info) for info in decoder_registry().decoders]


NODES: list[NodeSpec] = []  # filled lazily by the registry through nodes()
