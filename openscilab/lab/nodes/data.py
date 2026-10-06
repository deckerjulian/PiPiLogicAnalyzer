# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Data nodes: tables, buffers, files (read and write) and long-term loggers."""

from __future__ import annotations

import csv
import os
import time
from collections import deque
from typing import Any, Optional

import numpy as np

from ...core import capture_io, signals
from ..engine.runtime import NodeError, NodeRuntime
from .registry import In, Out, Param, collect, node

CAPTURE_EXTENSIONS = (".lac", ".lac.gz", ".sr", ".csv", ".vcd")

#: seconds after which the logger writes its rows to disk even when few came
FLUSH_SECONDS = 5.0


def row_of(value: Any, time: float) -> list[dict]:
    """Rows a value adds to a table."""
    if isinstance(value, signals.Table):
        return value.rows()
    if isinstance(value, dict):
        # a bundle: the plain values of its fields (a measurement's number, not its repr)
        return [{str(key): scalar_value(item) for key, item in value.items()}]
    if isinstance(value, signals.Scalar):
        return [{"time": value.at if value.at else time, "value": value.value}]
    if isinstance(value, signals.Bool):
        return [{"time": value.at if value.at else time, "value": bool(value.value)}]
    if isinstance(value, signals.Event):
        return [{"time": stamp, "data": data} for stamp, data in value]
    if isinstance(value, signals.Capture):
        return [{"time": value.start, "samples": value.sample_count, "channels": len(value.channels)}]
    if isinstance(value, (signals.Analog, signals.Digital)):
        # a block of samples: a row per sample
        values = np.asarray(value.values).tolist()
        stamps = np.asarray(value.times()).tolist() if value.time.is_known else [time] * len(values)
        return [{"time": stamp, "value": sample} for stamp, sample in zip(stamps, values)]
    if isinstance(value, (int, float, bool, str)):
        return [{"time": time, "value": value}]
    return [{"time": time, "value": repr(value)}]


def scalar_value(value: Any) -> Any:
    if isinstance(value, (signals.Scalar, signals.Bool)):
        return value.value
    if isinstance(value, signals.Event) and len(value):
        return value.data[-1]
    return value


def _column_ports(params: dict):
    return [In(str(name), signals.ANY, f"column {name}") for name in (params.get("columns") or [])], []


@node("data.table", title="Table",
      description="Collects values into a table. With 'columns', each column has an input and a row is "
                  "complete when every column got a value (values wait in the order they came, so a "
                  "column may run ahead); otherwise every value on 'in' adds rows.",
      inputs=[In("in", signals.ANY, multiple=True)], outputs=[Out("table", signals.TABLE)],
      params=[Param("columns", "list")], ports=_column_ports, icon="list")
class TableNode(NodeRuntime):
    async def setup(self) -> None:
        self.table = signals.Table(name=self.node.id)
        #: values per column waiting for the other columns of their row
        self.pending: dict[str, list[Any]] = {}

    async def on_input(self, port: str, value: Any) -> None:
        columns = list(self.p("columns") or [])
        if port == "in" or not columns:
            for row in row_of(value, self.ctx.now()):
                self.table.add_row(row)
            self.ctx.emit("table", self.table)
            return
        self.pending.setdefault(port, []).append(scalar_value(value))
        if all(self.pending.get(column) for column in columns):
            self.table.add_row({column: self.pending[column].pop(0) for column in columns})
            self.ctx.emit("table", self.table)


def write_table(path: str, table: signals.Table) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(table.columns))
        for values in zip(*table.columns.values()):
            writer.writerow(values)


def write_value(path: str, value: Any, append: bool = False) -> None:
    """Write a capture (by the file extension) or a table (CSV); other values are a line each of a
    text file (``append``: after the lines written before, else the file starts anew)."""
    lowered = path.lower()
    if isinstance(value, (signals.Digital, signals.Analog)):
        digital = isinstance(value, signals.Digital)
        name = value.name or ("D" if digital else "A")
        start = value.time.time_of(0) if value.time.is_known else 0.0
        value = signals.Capture(name=value.name, rate=value.rate or 1.0, start=start,
                                digital={name: value.values} if digital else {},
                                analog={} if digital else {name: value.values},
                                analog_units={} if digital else {name: value.unit or "V"})
    if isinstance(value, signals.Capture):
        session = value.to_session()
        if lowered.endswith((".lac", ".lac.gz")):
            capture_io.save_capture(path, session)
        elif lowered.endswith(".sr"):
            from ...core import sigrok_session

            sigrok_session.save_session(path, session)
        elif lowered.endswith(".csv"):
            capture_io.export_csv(path, session)
        elif lowered.endswith(".vcd"):
            capture_io.export_vcd(path, session)
        else:
            raise NodeError(f"{os.path.basename(path)}: captures are written as .lac, .sr, .csv or .vcd")
        return
    if isinstance(value, signals.Event):
        value = value.to_table()
    if isinstance(value, dict):
        value = signals.Table.from_rows(row_of(value, 0.0))
    if isinstance(value, signals.Table):
        if lowered.endswith((".lac", ".lac.gz", ".sr", ".vcd")):
            raise NodeError(f"{os.path.basename(path)}: a table is written as .csv (captures as .lac, .sr, .vcd)")
        write_table(path, value)
        return
    with open(path, "a" if append else "w", encoding="utf-8") as handle:
        handle.write(f"{scalar_value(value)}\n")


def last_number(path: str) -> int:
    """The highest number of the numbered files of ``path`` there are (0: none)."""
    import re

    before, after = os.path.basename(numbered(path, 0)).split("-000", 1)
    pattern = re.compile(re.escape(before) + r"-(\d+)" + re.escape(after) + "$")
    try:
        names = os.listdir(os.path.dirname(os.path.abspath(path)))
    except OSError:
        return 0
    return max((int(match.group(1)) for match in map(pattern.match, names) if match), default=0)


def numbered(path: str, index: int) -> str:
    for extension in (".lac.gz",) + CAPTURE_EXTENSIONS + (".txt",):
        if path.lower().endswith(extension):
            return f"{path[:-len(extension)]}-{index:03d}{path[-len(extension):]}"
    root, extension = os.path.splitext(path)
    return f"{root}-{index:03d}{extension}"


@node("data.file", title="File",
      description="Writes what arrives: captures as .lac/.sr/.csv/.vcd (by the extension), tables, bundles "
                  "and events as CSV, every value replacing the file; other values (numbers, text) are lines "
                  "of the file, one per value of the run. 'numbered' writes name-001, name-002, ... instead, "
                  "after the numbers already there.",
      inputs=[In("in", signals.ANY)], outputs=[Out("written", signals.EVENT)],
      params=[Param("path", "path", required=True), Param("numbered", "bool", False)], icon="save")
class FileNode(NodeRuntime):
    async def setup(self) -> None:
        path = self.ctx.path(str(self.p("path")))
        #: the number of the last numbered file (they go on after those of earlier runs)
        self.count = last_number(path) if self.p("numbered", False) else 0
        #: files written in this run (lines of values go on after the first one)
        self.written: set[str] = set()

    async def on_input(self, port: str, value: Any) -> None:
        path = self.ctx.path(str(self.p("path")))
        self.count += 1
        if self.p("numbered", False):
            path = numbered(path, self.count)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        await self.ctx.external(write_value, path, value, path in self.written)
        self.written.add(path)
        self.ctx.log(f"wrote {path}")
        self.ctx.emit("written", signals.Event(times=[self.ctx.now()], data=[path]))


def read_file(path: str):
    """A capture (.lac, .lac.gz, .sr) or a table (.csv)."""
    lowered = path.lower()
    if lowered.endswith((".lac", ".lac.gz")):
        session = capture_io.load_capture(path).session
        return signals.Capture.from_session(session)
    if lowered.endswith(".sr"):
        from ...core import sigrok_session

        return signals.Capture.from_session(sigrok_session.load_session(path))
    if lowered.endswith(".csv"):
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        table = signals.Table.from_rows([{key: _number(value) for key, value in row.items()} for row in rows],
                                        name=os.path.basename(path))
        return table
    raise NodeError(f"{os.path.basename(path)}: reads .lac, .lac.gz, .sr and .csv")


def _number(text: Any) -> Any:
    """A cell of a CSV file as the value it was written from (number, truth value, empty)."""
    if text in ("", None):
        return None
    if text in ("True", "False"):
        return text == "True"
    try:
        return int(text)
    except (TypeError, ValueError):
        try:
            return float(text)
        except (TypeError, ValueError):
            return text


@node("data.file_read", title="Read file",
      description="Reads a capture (.lac, .sr) or a table (.csv) when the flow starts, or on every value "
                  "at 'read'.",
      inputs=[In("read", signals.ANY)], outputs=[Out("capture", signals.CAPTURE), Out("table", signals.TABLE)],
      params=[Param("path", "path", required=True)], icon="folder")
class FileReadNode(NodeRuntime):
    async def run(self) -> None:
        if not self.ctx.wired("read"):
            await self.read()

    async def on_input(self, port: str, value: Any) -> None:
        await self.read()

    async def read(self) -> None:
        path = self.ctx.path(str(self.p("path")))
        if not os.path.exists(path):
            raise NodeError(f"{path} does not exist")
        value = await self.ctx.external(read_file, path)
        self.ctx.emit("capture" if isinstance(value, signals.Capture) else "table", value)


@node("data.buffer", title="Buffer",
      description="Keeps the latest values: the last 'length' seconds of a signal (blocks are joined) or "
                  "the last 'count' other values. Sends the buffer on every value, or only on 'read'.",
      inputs=[In("in", signals.ANY, optional=False, multiple=True), In("read", signals.ANY)],
      outputs=[Out("out", signals.ANY)],
      params=[Param("length", "quantity", "1 s", "s"), Param("count", "int", 100)], icon="layers")
class BufferNode(NodeRuntime):
    async def setup(self) -> None:
        self.signal = None
        self.values: deque = deque(maxlen=max(int(self.p("count", 100)), 1))

    async def on_input(self, port: str, value: Any) -> None:
        if port == "read":
            self.ctx.emit("out", self.current())
            return
        if isinstance(value, (signals.Digital, signals.Analog)) and value.time.is_uniform:
            if self.signal is None or type(self.signal) is not type(value) or self.signal.rate != value.rate:
                self.signal = type(value)(name=value.name, unit=value.unit, values=value.values.copy(), time=value.time)
            else:
                try:
                    self.signal.append(value)
                except signals.SignalError:
                    self.signal = type(value)(name=value.name, unit=value.unit, values=value.values.copy(),
                                              time=value.time)
            keep = int((self.q("length") or 1.0) * self.signal.rate)
            if len(self.signal) > keep:
                drop = len(self.signal) - keep
                self.signal.values = self.signal.values[drop:]
                self.signal.time = self.signal.time.sliced(drop, drop + keep)
        elif isinstance(value, signals.Capture):
            self.join_capture(value)
        else:
            self.values.append((self.ctx.now(), value))  # (stamped when it came)
        if not self.ctx.wired("read"):
            self.ctx.emit("out", self.current())

    def join_capture(self, block: signals.Capture) -> None:
        """Blocks of a stream become one capture of the last 'length' seconds."""
        def copy(capture: signals.Capture) -> signals.Capture:
            # the buffer's own name: a view does not take its windows for blocks of the stream
            return signals.Capture(name=self.node.id, rate=capture.rate, start=capture.start,
                                   digital={name: values.copy() for name, values in capture.digital.items()},
                                   analog={name: values.copy() for name, values in capture.analog.items()},
                                   analog_units=dict(capture.analog_units), trigger=0)

        current = self.signal
        if not isinstance(current, signals.Capture) or current.rate != block.rate \
                or set(current.channels) != set(block.channels):
            self.signal = copy(block)
        else:
            current.append(block)
        capture = self.signal
        keep = int((self.q("length") or 1.0) * capture.rate)
        drop = capture.sample_count - keep
        if drop > 0:
            capture.digital = {name: values[drop:] for name, values in capture.digital.items()}
            capture.analog = {name: values[drop:] for name, values in capture.analog.items()}
            capture.start += drop / capture.rate

    def current(self) -> Any:
        if self.signal is not None:
            return self.signal
        return signals.Table.from_rows([row for stamp, value in self.values for row in row_of(value, stamp)],
                                       name=self.node.id)


@node("data.logger", title="Logger",
      description="Records for a long time on disk. A .csv path: values, events and the new rows of tables "
                  "as lines time, source, value (a table with several columns: a line per column), written "
                  "at once and appended to the file of earlier runs. A .lac or .sr path: the samples of "
                  "streams and signals (digital and analog), in a capture on disk saved when the flow ends.",
      inputs=[In("in", signals.ANY, optional=False, multiple=True)], outputs=[Out("written", signals.EVENT)],
      params=[Param("path", "path", "log.csv", required=True), Param("flush", "int", 50)], icon="save")
class LoggerNode(NodeRuntime):
    async def setup(self) -> None:
        self.path = self.ctx.path(str(self.p("path")))
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.handle = None
        self.writer = None
        self.lines = 0
        #: rows written since the file was flushed, and when that was
        self.unflushed = 0
        self.flushed = time.monotonic()
        #: the rows of every table as they were logged last (a table arrives whole again when it
        #: grows or slides on: only rows after them are new)
        self.table_rows: dict[str, list[dict]] = {}
        self.store = None
        self.rate: Optional[float] = None
        self.start: Optional[float] = None
        self.names: list[str] = []
        #: the analog channels of the samples: blocks of volts, and their units
        self.analog: dict[str, list[np.ndarray]] = {}
        self.analog_units: dict[str, str] = {}

    def _csv(self):
        if self.handle is None:
            new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
            self.handle = open(self.path, "a", newline="", encoding="utf-8")  # noqa: SIM115 - kept open while running
            self.writer = csv.writer(self.handle)
            if new:
                self.writer.writerow(["time", "source", "value"])
        return self.writer

    @staticmethod
    def _new_rows(before: list[dict], rows: list[dict]) -> list[dict]:
        """The rows of a table that came after those logged before: it grew (the old rows lead it)
        or slid on (its first rows are the last ones logged)."""
        for overlap in range(min(len(before), len(rows)), 0, -1):
            if rows[:overlap] == before[-overlap:]:
                return rows[overlap:]
        return rows

    async def on_input(self, port: str, value: Any) -> None:
        if isinstance(value, (signals.Capture, signals.Digital, signals.Analog)) \
                and not self.path.lower().endswith(".csv"):
            self._samples(value)
            return
        writer = self._csv()
        now = self.ctx.now()
        rows = row_of(value, now)
        # where the value comes from: its name (the pin of a monitor, the node of a measurement)
        source = str(getattr(value, "name", "") or ("" if isinstance(value, dict) else self.node.id))
        if isinstance(value, signals.Table):
            before = self.table_rows.get(source, [])
            self.table_rows[source] = rows
            rows = self._new_rows(before, rows)
        lines = 0
        for row in rows:
            stamp = row.get("time", now)
            fields = {key: item for key, item in row.items() if key != "time"}
            if set(fields) <= {"value", "data"}:
                writer.writerow([stamp, source, fields.get("value", fields.get("data"))])
                lines += 1
            else:
                # several columns (a bundle, a table of measurements): a line per column
                for key, item in fields.items():  # (a bundle: its field names)
                    writer.writerow([stamp, f"{source}.{key}" if source else key, item])
                    lines += 1
        self.lines += lines
        self.unflushed += lines
        # on disk after 'flush' rows, and after a few seconds also when the values come slowly
        if self.unflushed >= max(int(self.p("flush", 50)), 1) or time.monotonic() - self.flushed >= FLUSH_SECONDS:
            self.handle.flush()
            self.unflushed = 0
            self.flushed = time.monotonic()

    def _samples(self, value) -> None:
        from ...core.sample_store import DiskAllocator, SampleStore, disk_sample_bytes

        if isinstance(value, signals.Capture):
            capture = value
        elif isinstance(value, signals.Digital):
            capture = signals.Capture(name=value.name, rate=value.rate or 1.0,
                                      start=value.time.time_of(0) if value.time.is_known else 0.0,
                                      digital={value.name or "D0": value.values})
        else:
            capture = signals.Capture(name=value.name, rate=value.rate or 1.0,
                                      start=value.time.time_of(0) if value.time.is_known else 0.0,
                                      analog={value.name or "A0": value.values},
                                      analog_units={value.name or "A0": value.unit or "V"})
        for name, values in capture.analog.items():
            self.analog.setdefault(name, []).append(np.asarray(values, dtype=np.float32))
            self.analog_units[name] = capture.analog_units.get(name, "V")
        if self.rate is None:
            self.rate, self.start = capture.rate, capture.start
        if not capture.digital:
            return
        if self.store is None:
            self.names = list(capture.digital)
            # The store grows in chunks on disk (memory mapped), so hours of samples fit.
            capacity = max(disk_sample_bytes() // max(len(self.names), 1), 1 << 20)
            self.store = SampleStore(list(range(len(self.names))), capacity, DiskAllocator())
        self.store.append({index: np.asarray(capture.digital[name], np.uint8) for index, name in enumerate(self.names)
                           if name in capture.digital})

    async def finish(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
            self.ctx.emit("written", signals.Event(times=[self.ctx.now()], data=[self.path]))
        if self.store is not None or self.analog:
            digital: dict[str, np.ndarray] = {}
            if self.store is not None:
                samples, _first = self.store.result()
                digital = {name: np.asarray(samples[index]) for index, name in enumerate(self.names)}
            analog = {name: np.concatenate(blocks) for name, blocks in self.analog.items()}
            count = min((len(values) for values in list(digital.values()) + list(analog.values())), default=0)
            capture = signals.Capture(name="log", rate=self.rate or 1.0, start=self.start or 0.0,
                                      digital={name: values[:count] for name, values in digital.items()},
                                      analog={name: values[:count] for name, values in analog.items()},
                                      analog_units=dict(self.analog_units))
            self.store = None
            self.analog = {}
            await self.ctx.external(write_value, self.path, capture)
            self.ctx.emit("written", signals.Event(times=[self.ctx.now()], data=[self.path]))

    def cleanup(self) -> None:
        # whatever ended the flow: the rows written so far are in the file
        handle, self.handle = getattr(self, "handle", None), None
        if handle is not None:
            handle.close()



NODES = collect(globals())
