# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The built-in generator of the DHO900 (output GI) as a :class:`GeneratorFacet`: standard
waveforms, arbitrary points, sweep and burst, through the ``:SOURce`` commands of
:data:`.scpi.COMMANDS` (provisional until step 3a)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .scpi import ScpiError, command
from ...core import waveform as waves
from ...core.instrument import CacheEntry, CacheFacet, GeneratorFacet, InstrumentError, OutputInfo

if TYPE_CHECKING:  # pragma: no cover
    from .driver import RigolDhoDriver

OUTPUT = "GI"
FUNCTIONS = {waves.SINE: "SINusoid", waves.SQUARE: "SQUare", waves.TRIANGLE: "RAMP", waves.RAMP: "RAMP",
             waves.PULSE: "PULSe", waves.DC: "DC", waves.NOISE: "NOISe", waves.ARBITRARY: "ARB"}
INFO = OutputInfo(OUTPUT, "analog", voltage_range=(-5.0, 5.0), resolution=14, max_rate=156_250_000,
                  max_points=16384, max_frequency=25_000_000,
                  capabilities=frozenset({"SWEEP", "BURST", "ARB"}))


class DhoGenerator(GeneratorFacet):
    def __init__(self, driver: "RigolDhoDriver") -> None:
        super().__init__()
        self.driver = driver
        self._running = False

    def outputs(self) -> list[OutputInfo]:
        return [INFO]

    def commands_for(self, waveform: Any) -> list[str]:
        """The SCPI commands that set ``waveform`` up (without switching the output on)."""
        found = waves.problems(waveform, INFO)
        if found:
            raise InstrumentError("; ".join(found))
        result = [command("afg_function", value=FUNCTIONS[waveform.kind])]
        if waveform.kind == waves.ARBITRARY:
            points = waveform.points if waveform.points is not None else np.zeros(1)
            result.append(command("afg_arb", value=",".join(f"{value:.4f}" for value in points)))
        if waveform.kind != waves.DC:
            result.append(command("afg_frequency", value=f"{waveform.frequency:.6g}"))
            result.append(command("afg_amplitude", value=f"{2 * abs(waveform.amplitude):.6g}"))
        result.append(command("afg_offset", value=f"{waveform.offset:.6g}"))
        if waveform.kind == waves.SQUARE:
            result.append(command("afg_square_duty", value=f"{waveform.duty * 100:.6g}"))
        if waveform.kind == waves.PULSE:
            result.append(command("afg_duty", value=f"{waveform.duty * 100:.6g}"))
        if waveform.phase:
            result.append(command("afg_phase", value=f"{waveform.phase:.6g}"))
        result.append(command("afg_sweep", value="ON" if waveform.modulation == waves.SWEEP else "OFF"))
        if waveform.modulation == waves.SWEEP:
            result += [command("afg_sweep_start", value=f"{waveform.sweep_start:.6g}"),
                       command("afg_sweep_stop", value=f"{waveform.sweep_stop:.6g}"),
                       command("afg_sweep_time", value=f"{waveform.sweep_time:.6g}")]
        result.append(command("afg_burst", value="ON" if waveform.modulation == waves.BURST else "OFF"))
        if waveform.modulation == waves.BURST:
            result += [command("afg_burst_cycles", value=int(waveform.burst_cycles)),
                       command("afg_burst_period", value=f"{waveform.burst_period:.6g}")]
        return result

    def start(self, output: str, waveform: Any) -> None:
        self.output(output)
        lines = self.commands_for(waveform)
        try:
            for line in lines:
                self.driver.connection.write(line)
            self.driver.connection.write(command("afg_output", value="ON"))
            errors = self.driver.connection.errors()
        except ScpiError as error:
            raise InstrumentError(f"the oscilloscope does not answer ({error})") from error
        if errors:
            raise InstrumentError("the oscilloscope refused the waveform: " + "; ".join(errors))
        self._running = True

    def stop(self, output: str) -> None:
        self.output(output)
        try:
            self.driver.connection.write(command("afg_output", value="OFF"))
            self.driver.connection.errors()  # a round trip: the output is off when this returns
        except ScpiError as error:
            raise InstrumentError(f"the oscilloscope does not answer ({error})") from error
        self._running = False

    def running(self, output: str) -> bool:
        return self._running


class DhoCache(CacheFacet):
    """The snapshot cache of the bridge app."""

    def __init__(self, driver: "RigolDhoDriver") -> None:
        super().__init__()
        from .fetcher import BridgeCache

        self.cache = BridgeCache(driver)

    def _call(self, function, *arguments):
        try:
            return function(*arguments)
        except ScpiError as error:
            raise InstrumentError(str(error)) from error

    def entries(self) -> list[CacheEntry]:
        return [CacheEntry(*item) for item in self._call(self.cache.entries)]

    def delete(self, entry_id=None) -> None:
        self._call(self.cache.delete, entry_id)

    def set_limit(self, size: int) -> None:
        self._call(self.cache.set_limit, size)
