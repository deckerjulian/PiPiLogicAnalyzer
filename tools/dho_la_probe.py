#!/usr/bin/env python3
# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Probe a Rigol DHO900 over SCPI before the driver is trusted.

Asks the instrument what the driver needs to know and stores every question, answer, error and
data block in ``dho-probe/<time>/``:

* identity, acquisition settings, memory depth, sample rate, the state of the logic analyzer;
* ``:WAVeform:PREamble?`` and the first block of ``:WAVeform:DATA?`` (RAW, BYTE) of D0–D15 and
  CH1–CH4, to see the format of digital and analog data;
* with ``--speed``: how fast RAW data come over the network (bytes per second);
* the commands of the built-in generator (``:SOURce``) the driver uses, read back.

Usage (LA on, CH1–CH4 on, a generator square wave on D0 and CH1, then SINGLE)::

    python tools/dho_la_probe.py 192.168.1.20 --stop --speed

The commands come from ``openscilab/driver/rigoldho/scpi.py`` (``COMMANDS``): what this probe finds
goes into that table.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openscilab.driver.rigoldho import scpi  # noqa: E402

QUERIES = [
    "*IDN?",
    ":TRIGger:STATus?",
    ":ACQuire:MDEPth?",
    ":ACQuire:SRATe?",
    ":ACQuire:TYPE?",
    ":TIMebase:MAIN:SCALe?",
    ":TIMebase:MAIN:OFFSet?",
    ":LA:STATe?",
    ":LA:POD1:THReshold?",
    ":LA:POD2:THReshold?",
    ":TRIGger:MODE?",
    ":TRIGger:SWEep?",
    ":TRIGger:EDGE:SOURce?",
    ":SOURce:FUNCtion?",
    ":SOURce:FREQuency?",
    ":SOURce:VOLTage:AMPLitude?",
    ":SOURce:OUTPut:STATe?",
    ":BRIDge:VERSion?",
]
SOURCES = [f"D{index}" for index in range(16)] + [f"CHANnel{index}" for index in range(1, 5)]


class Probe:
    def __init__(self, host: str, port: int, folder: Path, timeout: float) -> None:
        self.connection = scpi.ScpiConnection(host, port, timeout=timeout)
        self.folder = folder
        self.log: list[dict] = []
        folder.mkdir(parents=True, exist_ok=True)

    def ask(self, text: str, timeout: float = 3.0) -> str:
        started = time.monotonic()
        try:
            answer = self.connection.query(text, timeout=timeout)
        except scpi.ScpiError as error:
            answer = f"<no answer: {error}>"
            # a query the instrument does not know leaves nothing to read; start afresh
            self.reconnect()
        errors = self.errors()
        self.log.append({"query": text, "answer": answer, "seconds": round(time.monotonic() - started, 4),
                         "errors": errors})
        print(f"{text:32} {answer}" + (f"   errors: {errors}" if errors else ""))
        return answer

    def tell(self, text: str) -> list[str]:
        self.connection.write(text)
        errors = self.errors()
        self.log.append({"command": text, "errors": errors})
        return errors

    def errors(self) -> list[str]:
        try:
            return self.connection.errors()
        except scpi.ScpiError as error:
            return [f"<error queue unreadable: {error}>"]

    def reconnect(self) -> None:
        host, port, timeout = self.connection.host, self.connection.port, self.connection.timeout
        self.connection.close()
        self.connection = scpi.ScpiConnection(host, port, timeout=timeout)

    def waveforms(self, points: int) -> None:
        for source in SOURCES:
            self.tell(scpi.command("wave_source", value=source))
            self.tell(scpi.command("wave_mode"))
            self.tell(scpi.command("wave_format"))
            preamble = self.ask(scpi.command("wave_preamble?"))
            self.tell(scpi.command("wave_start", value=1))
            self.tell(scpi.command("wave_stop", value=points))
            try:
                data = self.connection.query_block(scpi.command("wave_data?"), timeout=30.0)
            except scpi.ScpiError as error:
                print(f"{source}: no data ({error})")
                self.reconnect()
                continue
            (self.folder / f"{source}.bin").write_bytes(data)
            values = sorted(set(data[:100_000]))
            summary = {"source": source, "preamble": preamble, "bytes": len(data),
                       "distinct values": values[:20], "count of distinct values": len(values)}
            self.log.append(summary)
            print(f"{source:10} {len(data):>9} bytes, values {values[:8]}{' …' if len(values) > 8 else ''}")

    def speed(self, source: str, total: int) -> float:
        self.tell(scpi.command("wave_source", value=source))
        self.tell(scpi.command("wave_mode"))
        self.tell(scpi.command("wave_format"))
        points = int(float(self.ask(":ACQuire:MDEPth?")) or total)
        received = 0
        started = time.monotonic()
        position = 1
        while received < total:
            stop = min(position + scpi.MAX_POINTS_PER_READ - 1, points)
            self.connection.write(scpi.command("wave_stop", value=stop))
            self.connection.write(scpi.command("wave_start", value=position))
            self.connection.write(scpi.command("wave_stop", value=stop))
            received += len(self.connection.query_block(scpi.command("wave_data?"), timeout=60.0))
            position = stop + 1 if stop < points else 1
        rate = received / (time.monotonic() - started)
        self.log.append({"speed": {"source": source, "bytes": received, "bytes per second": round(rate)}})
        print(f"{received:,} bytes of {source} at {rate / 1e6:.2f} MB/s")
        return rate

    def save(self) -> None:
        (self.folder / "probe.json").write_text(json.dumps(self.log, indent=1) + "\n")
        print(f"saved in {self.folder}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="address of the oscilloscope")
    parser.add_argument("--port", type=int, default=scpi.SCPI_PORT, help="SCPI port (5555; 5560: the bridge app)")
    parser.add_argument("--stop", action="store_true", help="stop the acquisition first (:STOP)")
    parser.add_argument("--speed", action="store_true", help="measure the RAW transfer rate")
    parser.add_argument("--speed-bytes", type=int, default=20_000_000, help="bytes the speed test reads")
    parser.add_argument("--points", type=int, default=100_000, help="points of the waveform read per source")
    parser.add_argument("--out", default="dho-probe", help="folder of the results")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    folder = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    try:
        probe = Probe(args.host, args.port, folder, args.timeout)
    except scpi.ScpiError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        if args.stop:
            probe.tell(scpi.command("stop"))
        for query in QUERIES:
            probe.ask(query)
        probe.waveforms(args.points)
        if args.speed:
            probe.speed("CHANnel1", args.speed_bytes)
            probe.speed("D0", args.speed_bytes)
    except scpi.ScpiError as error:
        print(f"stopped: {error}", file=sys.stderr)
    finally:
        probe.save()
        probe.connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
