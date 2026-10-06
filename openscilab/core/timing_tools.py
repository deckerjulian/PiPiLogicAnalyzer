# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The time of an instrument's samples outside a flow (the *Timing* tab of the device card).

* :func:`begin` / :func:`arrived` / :func:`finish` - the timing of a capture or stream the data
  view starts, as the flows keep it for theirs (``lab/nodes/device.py``);
* :func:`measure_latency` - the loopback of ``timing.calibrate`` without a flow: an output wired
  to a channel is switched while the instrument streams it; the latency is kept for the instrument;
* :class:`SyncOutput` - the sync signal of ``timing.sync`` on a pin, until it is stopped;
* :func:`device_config` - per instrument: whether its sample clock is shared with others or comes
  from outside (``timing.align`` then fits only the offset), the pin of its sync output.

Qt free; ``docs/timing.md``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

import numpy as np

from . import settings
from .instrument import MODE_OUTPUT, CaptureFacet, GpioFacet, Instrument, InstrumentError
from .timing import (
    TIMING_FILE,
    Acquisition,
    Latency,
    Loopback,
    calibration_key,
    edges,
    store_latency,
    stored_latency,
    timing_of,
)

#: ``clock`` of :func:`device_config`
CLOCK_OWN = "own"
CLOCK_SHARED = "shared"
CLOCKS = {CLOCK_OWN: "Own sample clock (offset and drift are fitted)",
          CLOCK_SHARED: "Shared or external sample clock (only the offset is fitted)"}


# ------------------------------------------------------------- settings
def _device_key(instrument: Any) -> str:
    return calibration_key(instrument, "", 0).rsplit("|", 2)[0]


def device_config(instrument: Any) -> dict:
    """The time settings of ``instrument``: ``clock`` (:data:`CLOCKS`), ``sync_pin``, ``sync_seed``."""
    data = settings.get_settings(TIMING_FILE) or {}
    entry = ((data.get("devices") or {}) if isinstance(data, dict) else {}).get(_device_key(instrument))
    config = {"clock": CLOCK_OWN, "sync_pin": "", "sync_seed": 1}
    if isinstance(entry, dict):
        config.update({key: value for key, value in entry.items() if key in config})
    if config["clock"] not in CLOCKS:
        config["clock"] = CLOCK_OWN
    return config


def set_device_config(instrument: Any, **values: Any) -> dict:
    """Change the time settings of ``instrument`` (see :func:`device_config`)."""
    config = device_config(instrument)
    config.update({key: value for key, value in values.items() if key in config})
    data = settings.get_settings(TIMING_FILE)
    data = data if isinstance(data, dict) else {}
    devices = dict(data.get("devices") or {})
    devices[_device_key(instrument)] = config
    data["devices"] = devices
    settings.persist_settings(TIMING_FILE, data)
    return config


def shares_clock(instrument: Any) -> bool:
    """Whether the sample clock of ``instrument`` is shared or external (only an offset to find)."""
    return instrument is not None and device_config(instrument)["clock"] == CLOCK_SHARED


# ----------------------------------------------- captures outside a flow
def simulator_of(instrument: Any):
    """The simulator of ``instrument`` when it knows the time of its samples (not when it emulates USB)."""
    model = getattr(instrument, "simulated_driver", None)
    return model if model is not None and getattr(model, "knows_time", True) else None


def begin(instrument: Any, session: Any, commanded: Optional[float] = None) -> Acquisition:
    """The timing of a capture or stream about to start (with the latency measured for it)."""
    from ..driver.base import ACQUISITION_STREAM
    from ..driver.models import TriggerType

    rate = float(session.frequency)
    stream = getattr(session, "acquisition_mode", "") == ACQUISITION_STREAM
    total = session.pre_trigger_samples + session.post_trigger_samples
    latency = None if simulator_of(instrument) is not None else stored_latency(
        calibration_key(instrument, "stream" if stream else "buffer", rate, total))
    acquisition = Acquisition(rate, time.monotonic() if commanded is None else commanded, latency,
                              name=getattr(instrument, "name", ""))
    if not stream and session.trigger_type != TriggerType.IMMEDIATE:
        acquisition.clock.started = None  # it waited for its trigger: the command bounds nothing
    return timing_of(instrument).begin(acquisition)


def started(acquisition: Acquisition, driver: Any) -> None:
    """A device process stamps the start command right before the device got it: a closer bound."""
    stamp = getattr(driver, "command_time", None)
    if stamp is not None and acquisition.clock.started is not None:
        acquisition.clock.start(max(float(stamp), acquisition.clock.started))


def arrived(acquisition: Acquisition, args: Any) -> None:
    """A block of a stream arrived (``CaptureProgressArgs``)."""
    if getattr(args, "sample_count", 0):
        acquisition.arrived(args.first_sample + args.sample_count - 1, getattr(args, "arrived", None))


def finish(acquisition: Acquisition, instrument: Any, args: Any) -> None:
    """The capture or stream completed (``CaptureCompletedArgs``): a simulator says when its first
    sample was taken, a capture arrives complete, a device may have stamped it in a time scale."""
    from ..driver.base import ACQUISITION_STREAM

    session = getattr(args, "session", None)
    if session is None or not getattr(args, "success", False):
        return
    model = simulator_of(instrument)
    if model is not None:
        offset = model.clock() - time.monotonic()
        if getattr(session, "acquisition_mode", "") == ACQUISITION_STREAM:
            acquisition.exact_start = model.last_stream_start - offset
        else:
            acquisition.exact_start = model.last_trigger_time - session.pre_trigger_samples / session.frequency - offset
        return
    if getattr(session, "acquisition_mode", "") != ACQUISITION_STREAM:
        total = session.pre_trigger_samples + session.post_trigger_samples
        acquisition.arrived(max(total - 1, 0), getattr(args, "arrived", None))
    start = getattr(session, "device_start_time", None)
    timescale = getattr(session, "device_timescale", None)
    if start is not None and timescale:
        acquisition.stamped(float(start), str(timescale), float(getattr(session, "device_time_accuracy", 0.0)))


# --------------------------------------------------------------- loopback
class LatencyError(RuntimeError):
    """The latency could not be measured (the message says why, for people)."""


def loopback_latency(samples: np.ndarray, rate: float, commands: list[float], acquisition: Acquisition,
                     repeats: int) -> tuple[Latency, int]:
    """Half the shortest loop from the commands (local times, right before each switch) to the edges
    of ``samples`` (the input, placed by ``acquisition``); returns the latency and the edges used."""
    loop = Loopback()
    _times, _levels, indexes = edges(np.asarray(samples), 0.0, rate)
    used = -1
    for command in commands:  # every command with the first edge after it (and after the last one)
        later = [index for index in indexes if index > used and acquisition.clock.envelope(index) >= command]
        if later:
            used = int(later[0])
            loop.edge(float(used), command)
    if loop.count < max(repeats // 2, 1):
        raise LatencyError(f"{loop.count} of {repeats} edges seen")
    return loop.latency(acquisition.clock), loop.count


def channel_of(instrument: Instrument, name: str) -> int:
    """The capture channel of ``name`` (a pin name, ``CH3`` or a number)."""
    pins = {pin.name: pin.channel for pin in instrument.pins() if pin.channel is not None}
    if name in pins:
        return int(pins[name])
    if name.upper().startswith("CH") and name[2:].isdigit():
        return int(name[2:]) - 1
    if name.isdigit():
        return int(name)
    raise LatencyError(f"{instrument.name} has no channel {name}")


def measure_latency(instrument: Instrument, pin: str, channel: str, rate: float = 100_000.0, mode: str = "stream",
                    samples: int = 100_000, repeats: int = 10, interval: float = 0.03, store: bool = True,
                    cancelled: Optional[Callable[[], bool]] = None) -> Latency:
    """The latency of ``instrument`` by a loopback from the output ``pin`` to ``channel`` (wired to
    each other): ``repeats`` switches while it streams (or captures) the channel. Blocks for about
    ``(repeats + 4) * interval``; kept for the instrument at this rate when ``store``. Its result goes
    to this function only (a data view of the instrument does not show it)."""
    from ..driver.base import ACQUISITION_STREAM, CaptureError
    from ..driver.models import AnalyzerChannel, CaptureSession, TriggerType

    gpio = instrument.facet(GpioFacet)
    capture = instrument.facet(CaptureFacet)
    if gpio is None or capture is None:
        raise LatencyError(f"{instrument.name} needs an output pin and capture channels")
    number = channel_of(instrument, channel)
    driver = capture.driver
    if driver.is_capturing:
        raise LatencyError(f"{instrument.name} is capturing; stop the capture first")
    total = int(samples) if mode == "capture" else int((repeats + 4) * interval * rate)
    session = CaptureSession(frequency=int(round(rate)), pre_trigger_samples=0, post_trigger_samples=total,
                             trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=number)]
    if mode == "stream":
        session.acquisition_mode = ACQUISITION_STREAM
    acquisition = Acquisition(rate, time.monotonic())
    commands: list[float] = []
    done = threading.Event()
    result: list = []

    def progress(args) -> None:
        if args.session is session:
            acquisition.arrived(args.first_sample + args.sample_count - 1, args.arrived or time.monotonic())

    def completed(args) -> None:
        if args.session is not session:
            return
        if mode == "capture":
            acquisition.arrived(total - 1, getattr(args, "arrived", None) or time.monotonic())
        result.append(args)
        done.set()

    try:
        if gpio.mode(pin) != MODE_OUTPUT:
            gpio.set_mode(pin, MODE_OUTPUT)
        gpio.write(pin, 0)
    except InstrumentError as error:
        raise LatencyError(f"{pin}: {error}") from None
    time.sleep(interval)
    driver.add_capture_progress_handler(progress)
    try:
        acquisition.clock.start(time.monotonic())
        error = driver.start_capture(session, completed)  # (only here: not to the data view)
        if error != CaptureError.NONE:
            raise LatencyError(f"the {mode} could not be started ({error.message})")
        started(acquisition, driver)
        time.sleep(interval)
        for index in range(repeats):
            if cancelled is not None and cancelled():
                driver.stop_capture()
                raise LatencyError("cancelled")
            commands.append(time.monotonic())  # (right before the command)
            gpio.write(pin, (index + 1) % 2)
            time.sleep(interval)
        if not done.wait(max(10.0, 4 * (repeats + 4) * interval)):
            driver.stop_capture()
            raise LatencyError(f"the {mode} did not end")
    finally:
        driver.remove_capture_progress_handler(progress)
        try:
            gpio.write(pin, 0)
        except InstrumentError:
            pass
    args = result[0]
    if not args.success:
        raise LatencyError(f"the {mode} failed: {args.error}")
    try:
        latency, _count = loopback_latency(args.session.capture_channels[0].samples, rate, commands, acquisition,
                                           repeats)
    except LatencyError as error:
        raise LatencyError(f"{error} on {channel} - is {pin} wired to it?") from None
    if store:
        store_latency(calibration_key(instrument, "stream" if mode == "stream" else "buffer", rate, total), latency)
    return latency


# ------------------------------------------------------------ sync output
class SyncOutput:
    """The sync signal of ``timing.sync`` on ``pin`` of ``instrument``, in a thread of its own until
    :meth:`stop`: edges at irregular intervals (the same for the same ``seed``). :attr:`edges` are
    the local times (``time.monotonic``, right before each command) and levels."""

    def __init__(self, instrument: Instrument, pin: str, seed: int = 1, minimum: float = 0.02,
                 maximum: float = 0.06) -> None:
        gpio = instrument.facet(GpioFacet)
        if gpio is None:
            raise InstrumentError(f"{instrument.name} has no GPIO")
        self.instrument, self.pin, self.seed = instrument, pin, int(seed)
        self.minimum, self.maximum = minimum, maximum
        self.gpio = gpio
        self.edges: list[tuple[float, int]] = []
        self.error = ""
        self._stop = threading.Event()
        if gpio.mode(pin) != MODE_OUTPUT:
            gpio.set_mode(pin, MODE_OUTPUT)
        gpio.write(pin, 0)
        self._thread = threading.Thread(target=self._run, name=f"sync-{pin}", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        from openscilab_device.timing import sync_intervals

        intervals = sync_intervals(self.seed, self.minimum, self.maximum)
        level = 0
        due = time.monotonic() + next(intervals)
        while not self._stop.wait(max(due - time.monotonic(), 0.0)):
            level ^= 1
            at = time.monotonic()
            try:
                self.gpio.write(self.pin, level)
            except Exception as error:  # noqa: BLE001 - a device that is gone ends the signal
                self.error = str(error)
                return
            self.edges.append((at, level))
            del self.edges[:-10_000]
            due += next(intervals)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(1.0)
        try:
            self.gpio.write(self.pin, 0)
        except Exception:  # noqa: BLE001 - best effort
            pass


_sync_outputs: dict[int, SyncOutput] = {}


def sync_output_of(instrument: Any) -> Optional[SyncOutput]:
    output = _sync_outputs.get(id(instrument))
    return output if output is not None and output.instrument is instrument else None


def start_sync_output(instrument: Instrument, pin: str, seed: int = 1) -> SyncOutput:
    stop_sync_output(instrument)
    output = SyncOutput(instrument, pin, seed)
    _sync_outputs[id(instrument)] = output
    return output


def stop_sync_output(instrument: Any) -> None:
    output = _sync_outputs.pop(id(instrument), None)
    if output is not None:
        output.stop()
