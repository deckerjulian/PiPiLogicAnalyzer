# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A simulated Rigol DHO900 on a local TCP port: its SCPI server and, optionally, the bridge app.

:class:`ScpiShell` answers the commands of :data:`openscilab.driver.rigoldho.scpi.COMMANDS` with
the device model of the simulator profile ``dho924s``: ``:SINGle`` captures, ``:WAVeform:DATA?``
returns its samples (digital channels one byte per sample, analog channels 8 bit raw), the
generator drives CH1. With ``bridge=True`` it also answers the ``:BRIDge`` commands of
``docs/protocols.md`` (snapshot cache, overviews, compressed tiles). Unknown commands go to the
error queue and get no answer, as on the instrument. Not simulated: edge triggers on the analog
channels (refused with a settings conflict) and the logic thresholds (stored and reported only:
the digital channels read 1 above half of the logic level).

Addresses: ``rigol-sim:direct`` and ``rigol-sim:bridge``.
"""

from __future__ import annotations

import itertools
import logging
import re
import socketserver
import threading
import time
from typing import Optional

import numpy as np

from ..base import CaptureError
from ..models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from ..rigoldho import codec, scpi
from ...core import waveform as waves

log = logging.getLogger("openscilab.driver")

#: volts of one step of the 8 bit raw values, and the raw value of 0 V
Y_INC = 10.0 / 256
Y_REF = 128.0


def _pattern(template: str) -> re.Pattern:
    text = re.escape(template).replace(r"\{n\}", r"(\d+)").replace(r"\{value\}", r"(.+)")
    return re.compile(f"^{text}$", re.IGNORECASE)


PATTERNS = {name: _pattern(template) for name, template in scpi.COMMANDS.items()}
FUNCTIONS = {"SINUSOID": waves.SINE, "SQUARE": waves.SQUARE, "RAMP": waves.TRIANGLE, "PULSE": waves.PULSE,
             "DC": waves.DC, "NOISE": waves.NOISE, "ARB": waves.ARBITRARY}


class _Snapshot:
    def __init__(self, number: int, digital: Optional[np.ndarray], analog: dict[int, np.ndarray], meta: dict) -> None:
        self.id = str(number)
        self.time = int(time.time())
        self.digital = digital
        self.analog = analog
        self.meta = meta
        self.points = int(meta["points"])


class ScpiShell:
    def __init__(self, bridge: bool = False) -> None:
        from . import open_simulated

        self.bridge = bridge
        self.instrument = open_simulated("dho924s")
        self.device = self.instrument.simulated_driver
        self.device.profile = dict(self.device.profile, progressive=None)  # the instrument has it all at once
        self.lock = threading.RLock()
        self.errors: list[str] = []
        self.status = "STOP"
        self.la_on = False
        self.digital_on: set[int] = set()
        self.analog_on: set[int] = set()
        self.depth = 10_000
        self.scale = 1e-3
        self.offset = 0.0
        self.trigger: dict[str, str] = {"sweep": "AUTO", "mode": "EDGE", "source": "D0", "slope": "POSITIVE"}
        self.thresholds: dict[int, float] = {}
        self.data_digital: Optional[np.ndarray] = None
        self.data_analog: dict[int, np.ndarray] = {}
        self.rate = 1e6
        self.trigger_index = 0
        self.source = "D0"
        self.window = (1, 1000)
        self.afg: dict[str, str] = {}
        self.snapshots: list[_Snapshot] = []
        self.cache_limit = 1 << 30
        self._numbers = itertools.count(1)
        shell = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for raw in self.rfile:
                    line = raw.decode("ascii", "replace").strip()
                    if not line:
                        continue
                    try:
                        answer = shell.handle(line)
                    except Exception as error:  # noqa: BLE001 - an error of the simulation: in the queue
                        log.debug("Simulated DHO: %s failed: %s", line, error)
                        shell.errors.append(f'-200,"{error}"')
                        answer = None
                    if answer is None:
                        continue
                    data = answer if isinstance(answer, bytes) else answer.encode() + b"\n"
                    try:
                        self.wfile.write(data)
                    except OSError:
                        return

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="openscilab-dho-shell", daemon=True).start()

    # ------------------------------------------------------------ commands
    def handle(self, line: str):
        with self.lock:
            if line.upper().startswith(":BRID"):
                if not self.bridge:
                    self.errors.append('-113,"Undefined header"')
                    return None
                return self._bridge(line)
            for name, pattern in PATTERNS.items():
                match = pattern.match(line)
                if match:
                    return self._command(name, match.groups())
            self.errors.append('-113,"Undefined header"')
            return None

    def _command(self, name: str, groups: tuple):
        value = groups[-1] if groups else ""
        if name == "identify":
            return "RIGOL TECHNOLOGIES,DHO924S,DHO9SIM0001,00.01.02"
        if name == "error":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if name == "clear":
            self.errors.clear()
        elif name in ("run", "stop"):
            if name == "stop" and self.device.is_capturing:
                self.device.stop_capture()
            self.status = "STOP" if name == "stop" else "RUN"
        elif name == "single":
            self._single()
        elif name == "status":
            return self.status
        elif name == "la_state":
            self.la_on = value.upper() in ("ON", "1")
        elif name == "la_channel":
            (self.digital_on.add if value.upper() in ("ON", "1") else self.digital_on.discard)(int(groups[0]))
        elif name == "la_threshold":
            self.thresholds[int(groups[0])] = float(value)
        elif name == "channel":
            (self.analog_on.add if value.upper() in ("ON", "1") else self.analog_on.discard)(int(groups[0]) - 1)
        elif name == "memory_depth":
            self.depth = int(float(value))
        elif name == "memory_depth?":
            return str(self.depth)
        elif name == "sample_rate?":
            return f"{self.rate:.6e}"
        elif name == "timebase_scale":
            self.scale = float(value)
        elif name == "timebase_offset":
            self.offset = float(value)
        elif name == "trigger_sweep":
            self.trigger["sweep"] = value.upper()
        elif name == "trigger_mode":
            self.trigger["mode"] = value.upper()
        elif name == "edge_source":
            self.trigger["source"] = value.upper()
        elif name == "edge_slope":
            self.trigger["slope"] = value.upper()
        elif name == "edge_level":
            self.trigger["level"] = value
        elif name == "pattern":
            self.trigger["pattern"] = value.upper()
        elif name == "wave_source":
            self.source = value.upper().replace("CHANNEL", "CHAN")
        elif name == "wave_start":
            self.window = (int(value), self.window[1])
        elif name == "wave_stop":
            self.window = (self.window[0], int(value))
        elif name == "wave_preamble?":
            points = self._points()
            return (f"0,2,{points},1,{1 / self.rate:.9e},{-self.trigger_index / self.rate:.9e},0,"
                    f"{Y_INC:.9e},0,{Y_REF:g}")
        elif name == "wave_data?":
            values = self._source_values()
            first, last = self.window
            return scpi.block(values[first - 1:last].tobytes())
        elif name.startswith("afg_"):
            return self._afg(name, value)
        return None

    def _points(self) -> int:
        if self.data_digital is not None:
            return len(self.data_digital)
        return len(next(iter(self.data_analog.values()))) if self.data_analog else 0

    def _source_values(self) -> np.ndarray:
        if self.source.startswith("D"):
            number = int(self.source[1:])
            if self.data_digital is None:
                return np.zeros(0, np.uint8)
            return ((self.data_digital >> np.uint16(number)) & 1).astype(np.uint8)
        number = int(self.source[4:]) - 1
        return self.data_analog.get(number, np.zeros(0, np.uint8))

    # --------------------------------------------------------------- capture
    def _single(self) -> None:
        total = self.depth
        rate = min(total / max(self.scale * scpi.DIVISIONS, 1e-12), float(self.device.max_frequency))
        session = CaptureSession(frequency=max(int(round(rate)), 1))
        pre = int(round(total / 2 - self.offset * session.frequency))
        session.pre_trigger_samples = min(max(pre, 0), total - 1)
        session.post_trigger_samples = total - session.pre_trigger_samples
        numbers = sorted(self.digital_on) if self.la_on else []
        if not numbers:
            numbers = [0]  # the samples of the device need a digital channel; dropped below
        session.capture_channels = [AnalyzerChannel(channel_number=number) for number in numbers]
        session.analog_channels = [AnalogChannel(channel_number=number) for number in sorted(self.analog_on)]
        mode = self.trigger["mode"]
        if self.trigger["sweep"] == "AUTO":
            session.trigger_type = TriggerType.IMMEDIATE
        elif mode == "EDGE" and not self.trigger["source"].startswith("D"):
            # (the instrument triggers on its analog channels too; the simulator does not)
            self.errors.append(f'-221,"Settings conflict: the simulator triggers on D0-D15 only, not on '
                               f'{self.trigger["source"]}"')
            self.status = "STOP"
            return
        elif mode == "EDGE" and self.trigger["source"].startswith("D"):
            session.trigger_type = TriggerType.EDGE
            session.trigger_channel = int(self.trigger["source"][1:])
            session.trigger_inverted = self.trigger["slope"].startswith("NEG")
            if session.trigger_channel not in numbers:
                session.capture_channels.append(AnalyzerChannel(channel_number=session.trigger_channel))
        elif mode.startswith("PATT"):
            levels = self.trigger.get("pattern", "").split(",")[4:20]
            used = [index for index, level in enumerate(levels) if level in ("H", "L")]
            if used:
                session.trigger_type = TriggerType.COMPLEX
                session.trigger_channel = min(used)
                session.trigger_bit_count = max(used) - min(used) + 1
                session.trigger_pattern = sum(1 << (index - min(used)) for index in used if levels[index] == "H")
                session.trigger_mask = sum(1 << (index - min(used)) for index in used)  # (X between them)
                present = {channel.channel_number for channel in session.capture_channels}
                for index in range(min(used), max(used) + 1):
                    if index not in present:
                        session.capture_channels.append(AnalyzerChannel(channel_number=index))
            else:
                session.trigger_type = TriggerType.IMMEDIATE
        else:
            session.trigger_type = TriggerType.IMMEDIATE
        self.status = "WAIT"
        self.rate = session.frequency
        error = self.device.start_capture(session, lambda args: self._captured(args, numbers))
        if error != CaptureError.NONE:
            self.errors.append(f'-221,"Settings conflict: {error.value}"')
            self.status = "STOP"

    def _captured(self, args, numbers: list[int]) -> None:
        with self.lock:
            session = args.session
            if not args.success:
                self.status = "STOP"
                return
            words = np.zeros(session.pre_trigger_samples + session.post_trigger_samples, dtype=np.uint16)
            for channel in session.capture_channels:
                if channel.channel_number in self.digital_on and self.la_on:
                    words[:len(channel.samples)] |= channel.samples.astype(np.uint16) << np.uint16(channel.channel_number)
            self.data_digital = words if self.la_on and self.digital_on else None
            scale, offset, _bits = self.device.analog_scale()
            self.data_analog = {}
            for channel in session.analog_channels:
                volts = channel.raw.astype(np.float64) * scale + offset
                self.data_analog[channel.channel_number] = np.clip(np.round(volts / Y_INC + Y_REF), 0, 255).astype(np.uint8)
            self.trigger_index = session.pre_trigger_samples
            self.rate = session.frequency
            self.status = "STOP"

    # ------------------------------------------------------------ generator
    def _afg(self, name: str, value: str):
        generator = self.instrument.generator
        if name == "afg_output?":
            return "1" if generator.running("GI") else "0"
        if name == "afg_output":
            if value.upper() in ("ON", "1"):
                generator.start("GI", self._afg_waveform())
            else:
                generator.stop("GI")
            return None
        self.afg[name] = value
        return None

    def _afg_waveform(self) -> waves.Waveform:
        settings = self.afg
        kind = FUNCTIONS.get(settings.get("afg_function", "SINUSOID").upper(), waves.SINE)
        options = dict(frequency=float(settings.get("afg_frequency", 1000)),
                       amplitude=float(settings.get("afg_amplitude", 2.0)) / 2,
                       offset=float(settings.get("afg_offset", 0.0)))
        if kind == waves.SQUARE and "afg_square_duty" in settings:
            options["duty"] = float(settings["afg_square_duty"]) / 100
        if kind == waves.PULSE and "afg_duty" in settings:
            options["duty"] = float(settings["afg_duty"]) / 100
        if settings.get("afg_sweep", "OFF").upper() == "ON":
            options.update(modulation=waves.SWEEP, sweep_start=float(settings.get("afg_sweep_start", 100)),
                           sweep_stop=float(settings.get("afg_sweep_stop", 1e4)),
                           sweep_time=float(settings.get("afg_sweep_time", 1.0)))
        if settings.get("afg_burst", "OFF").upper() == "ON":
            options.update(modulation=waves.BURST, burst_cycles=int(float(settings.get("afg_burst_cycles", 1))),
                           burst_period=float(settings.get("afg_burst_period", 0.01)))
        if kind == waves.ARBITRARY:
            points = [float(item) for item in settings.get("afg_arb", "0").split(",")]
            return waves.from_points(points, frequency=options.pop("frequency"),
                                     **{key: value for key, value in options.items() if key == "modulation"})
        return waves.Waveform(kind=kind, **options)

    # ---------------------------------------------------------------- bridge
    def _bridge(self, line: str):
        header, _, argument = line.partition(" ")
        header = header.upper()
        arguments = [item.strip() for item in argument.split(",")] if argument.strip() else []
        if header.startswith(":BRID:VERS") or header.startswith(":BRIDGE:VERS"):
            return "OPENSCILAB_BRIDGE,0.1.0-sim,1"
        if ":BENC" in header:
            return "25000000"
        if ":SNAP" in header:
            return self._snapshot(arguments)
        if ":CACH" in header and ":LIST" in header:
            return ";".join(f"{item.id},{item.time},{item.points}" for item in reversed(self.snapshots))
        if ":CACH" in header and ":DEL" in header:
            if arguments and arguments[0].upper() == "ALL":
                self.snapshots.clear()
            else:
                self.snapshots = [item for item in self.snapshots if item.id not in arguments]
            return "OK"
        if ":CACH" in header and ":LIM" in header:
            self.cache_limit = int(float(arguments[0]))
            return "OK"
        if ":META" in header:
            item = self._find(arguments[0])
            return ";".join(f"{key}={value}" for key, value in item.meta.items()) if item else "ERR unknown snapshot"
        if ":OVER" in header:
            item = self._find(arguments[0])
            if item is None:
                return "ERR unknown snapshot"
            values = self._channel(item, arguments[1])
            level = int(arguments[2])
            if arguments[1].upper() == "D":
                return scpi.block(codec.overview_digital(values, level).astype("<u2").tobytes())
            return scpi.block(codec.overview_analog(values, level).astype(np.uint8).tobytes())
        if ":TILE" in header:
            item = self._find(arguments[0])
            if item is None:
                return "ERR unknown snapshot"
            values = self._channel(item, arguments[1])
            start, count = int(arguments[2]), int(arguments[3])
            part = values[start:start + count]
            encoded = codec.encode_digital(part) if arguments[1].upper() == "D" else codec.encode_analog(part)
            return scpi.block(encoded)
        return "ERR unknown bridge command"

    def _find(self, number: str) -> Optional[_Snapshot]:
        return next((item for item in self.snapshots if item.id == number), None)

    @staticmethod
    def _channel(item: _Snapshot, name: str) -> np.ndarray:
        if name.upper() == "D":
            return item.digital if item.digital is not None else np.zeros(item.points, np.uint16)
        return item.analog[int(name.upper().replace("CH", "")) - 1]

    def _snapshot(self, arguments: list[str]) -> str:
        if self.status != "STOP" or (self.data_digital is None and not self.data_analog):
            return "ERR no stopped acquisition"
        points = self._points()
        meta = {"points": points, "rate": f"{self.rate:g}", "trigger": self.trigger_index,
                "channels": ",".join(arguments)}
        analog = {}
        for name in arguments:
            if name.upper().startswith("CH"):
                number = int(name[2:]) - 1
                if number in self.data_analog:
                    analog[number] = self.data_analog[number]
                    meta[f"CH{number + 1}.yinc"] = f"{Y_INC:.9e}"
                    meta[f"CH{number + 1}.yorig"] = "0"
                    meta[f"CH{number + 1}.yref"] = f"{Y_REF:g}"
        digital = self.data_digital if any(name.upper().startswith("D") for name in arguments) else None
        item = _Snapshot(next(self._numbers), digital, analog, meta)
        self.snapshots.append(item)
        return item.id

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.instrument.close()


def open_shell(mode: str = "bridge"):
    """A :class:`~openscilab.driver.rigoldho.driver.RigolDhoDriver` connected to a simulated DHO
    (``mode``: ``bridge`` or ``direct``); disposing the driver ends the simulation."""
    from ..rigoldho.driver import RigolDhoDriver

    if mode not in ("bridge", "direct"):
        raise ValueError(f"unknown simulated DHO {mode!r} (bridge, direct)")
    shell = ScpiShell(bridge=mode == "bridge")
    try:
        driver = RigolDhoDriver("127.0.0.1", shell.port, address=f"rigol-sim:{mode}")
    except Exception:
        shell.close()
        raise
    driver.shell = shell
    original = driver.dispose

    def dispose() -> None:
        original()
        shell.close()

    driver.dispose = dispose
    return driver

