# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Demo devices: try remote devices without hardware, here or on another computer::

    python -m openscilab_device.demo climate --server 192.168.1.20 --token ...
    python -m openscilab_device.demo audio            # finds openSciLab by its beacon

``climate``: a climate station - temperature and humidity (slow values), a door contact, a heater
and a fan to switch, a calibration command and a sync output. ``audio``: a microphone - 8 kHz
samples in blocks (a 440 Hz tone with noise) and a sync output. ``echo``: sends back every value it
is given, stamped when it applied it (to look at latencies). ``daq``: a measuring box behind USB as
a NI DAQ is - an analog input, a digital line recording openSciLab's sync signal, a digital output
wired back to a digital input (loopback) and a command that measures the latency with it; its
sample clock drifts against the computer's, its blocks arrive in USB frames, late and uneven.

openSciLab's simulator ``remote-sim:<name>`` runs the same devices in the application, with a
clock of their own and an emulated network in between.
"""

from __future__ import annotations

import argparse
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .device import Device

#: the demo devices: name -> function that builds and drives one (see :func:`build`)
DEMOS = ("climate", "audio", "echo", "daq")


def build(name: str, write_sync: Optional[Callable[[int], None]] = None, read: Optional[Callable] = None,
          write: Optional[Callable] = None, shared_clock: bool = False, **options) -> tuple[Device, Callable]:
    """The device ``name`` and the function that drives it (runs until the device stops; call it in a
    thread after ``start()``). ``write_sync``: what the sync output switches (a GPIO pin; default: nothing).
    ``daq``: ``read(net, start, rate, count, analog)`` gives what its inputs see (``start``: seconds of
    ``time.monotonic``), ``write(net, level, at)`` what its output drives; ``shared_clock``: its sample
    clock comes from openSciLab's instrument (no drift)."""
    device = Device(name, **options)
    sync = write_sync or (lambda level: None)
    if name == "climate":
        return device, _climate(device, sync)
    if name == "audio":
        return device, _audio(device, sync)
    if name == "echo":
        return device, _echo(device)
    if name == "daq":
        return device, _daq(device, read, write, shared_clock)
    raise ValueError(f"no demo device {name!r} (demos: {', '.join(DEMOS)})")


def _climate(device: Device, sync: Callable[[int], None]) -> Callable:
    device.description_text = "A climate station: temperature, humidity, a door, a heater and a fan."
    temperature = device.input("temperature", kind="scalar", unit="°C", description="air temperature, 10 per second")
    humidity = device.input("humidity", kind="scalar", unit="%", description="relative humidity, twice a second")
    door = device.input("door", kind="bool", description="door contact (true: open)")
    heater = device.output("heater", kind="scalar", unit="W", range=(0, 100), default=0.0,
                           description="heating power")
    device.output("fan", kind="bool", default=False, description="fan on or off")
    device.sync_output("SYNC", sync, description="toggles at irregular intervals while openSciLab asks for it")
    state = {"offset": 0.0}

    @device.command(description="Sets the sensor's offset so that it shows 'reference' now; returns the offset.")
    def calibrate(reference: float = 21.0) -> float:
        state["offset"] = float(reference) - state.get("last", reference)
        return round(state["offset"], 3)

    def drive() -> None:
        start = device.now()
        warm, step = 21.0, 0
        while not device._stop.is_set():
            now = device.now()
            # the heater warms the room slowly, the fan cools it
            power = float(heater.value or 0.0)
            fan = bool(device.outputs["fan"].value)
            warm += (power * 0.002 - (warm - 21.0) * (0.02 if fan else 0.005)) * 0.1
            value = warm + 0.4 * math.sin(2 * math.pi * (now - start) / 20) + random.gauss(0, 0.02) + state["offset"]
            state["last"] = value - state["offset"]
            temperature.send(round(value, 3), at=now)
            if step % 5 == 0:
                humidity.send(round(45 + 5 * math.sin(2 * math.pi * (now - start) / 60) + random.gauss(0, 0.2), 2), at=now)
            if step % 30 == 0:
                door.send((step // 30) % 4 == 1, at=now)
            step += 1
            device._stop.wait(max(start + step * 0.1 - device.now(), 0.0))

    return drive


def _audio(device: Device, sync: Callable[[int], None]) -> Callable:
    device.description_text = "A microphone: 8000 samples per second in blocks of 50 ms."
    rate, block = 8000.0, 400
    microphone = device.input("mic", kind="analog", rate=rate, unit="V", description="440 Hz tone with noise")
    level = device.input("level", kind="scalar", unit="V", description="RMS of every block")
    device.sync_output("SYNC", sync)

    def drive() -> None:
        start = device.now()
        index = 0
        while not device._stop.is_set():
            t0 = start + index / rate
            samples = [0.5 * math.sin(2 * math.pi * 440 * (t0 + n / rate)) + random.gauss(0, 0.02) for n in range(block)]
            # a block is sent when its last sample was taken
            device._stop.wait(max(t0 + block / rate - device.now(), 0.0))
            microphone.send_block(samples, t0=t0)
            level.send(round(math.sqrt(sum(v * v for v in samples) / block), 4), at=t0 + block / rate)
            index += block

    return drive


def _echo(device: Device) -> Callable:
    device.description_text = "Sends back every value it is given, stamped when it applied it."
    echo = device.input("echo", kind="scalar", description="the value of 'value', when it was applied")
    value = device.output("value", kind="scalar", default=0.0)
    value.on_set(lambda v, at: echo.send(v))

    def drive() -> None:
        device._stop.wait()

    return drive


@dataclass
class Usb:
    """How the emulated USB delivers (seconds): blocks leave at the next frame after their last
    sample and arrive ``latency`` plus an exponential ``jitter`` later; a command reaches the box
    after ``command`` plus up to ``command_jitter``."""

    frame: float = 0.001
    latency: float = 0.0012
    jitter: float = 0.0004
    command: float = 0.0006
    command_jitter: float = 0.0003


def _daq(device: Device, read: Optional[Callable], write: Optional[Callable], shared_clock: bool,
         usb: Optional[Usb] = None, rate: float = 10_000.0, block: int = 100, drift: float = 25e-6) -> Callable:
    device.description_text = ("A measuring box behind USB: AI0, a sync line, DO0 wired back to the line LOOP "
                               f"({rate:g} samples per second, blocks of {block}).")
    usb = usb or Usb()
    ai0 = device.input("ai0", kind="analog", rate=rate, unit="V", range=(-10, 10), description="analog input")
    sync_line = device.input("sync", kind="digital", rate=rate, sync=True, clock="shared" if shared_clock else None,
                             description="records openSciLab's sync signal")
    loop = device.input("loop", kind="digital", rate=rate, description="DO0, wired back")
    do0 = device.output("do0", kind="bool", default=False, description="digital output (wired to LOOP)")
    random_ = random.Random(7)
    period = 1.0 / (rate * (1.0 + (0.0 if shared_clock else drift)))  # the sample clock, in true seconds
    levels: list = [(0.0, 0)]  # (true time, level) of DO0
    lock = threading.Lock()

    def set_output(level: int) -> None:
        """A command to the box: DO0 changes a little later (USB)."""
        at = time.monotonic() + usb.command + random_.uniform(0, usb.command_jitter)
        with lock:
            levels.append((at, int(level)))
        if write is not None:
            write("DO0", int(level), at)

    do0.on_set(lambda value, at: set_output(int(bool(value))))

    @device.command(description="Measures the latency of the inputs with the loopback DO0 -> LOOP; returns it.")
    def calibrate_latency(repeats: int = 10) -> dict:
        latency = loop.calibrate(set_output, repeats=int(repeats), interval=0.03)
        for port in (ai0, sync_line):
            port.latency = latency  # one task, one USB pipe: the same latency
        return {"latency": latency.value, "uncertainty": latency.uncertainty}

    def level_at(at: float) -> int:
        with lock:
            current = 0
            for when, level in levels:
                if when <= at:
                    current = level
            return current

    def drive() -> None:
        for port in (ai0, sync_line, loop):
            port.start()  # right before the command that starts the box
        start = time.monotonic() + usb.command + random_.uniform(0, usb.command_jitter)
        index = 0
        while not device._stop.is_set():
            first = start + index * period
            last = first + (block - 1) * period
            arrive = math.ceil(last / usb.frame) * usb.frame + usb.latency + random_.expovariate(1.0 / usb.jitter)
            device._stop.wait(max(arrive - time.monotonic(), 0.0))
            if device._stop.is_set():
                return
            times = [first + n * period for n in range(block)]
            if read is not None:
                analog = read("AI0", first, 1.0 / period, block, True)
                synced = read("SYNC_IN", first, 1.0 / period, block, False)
            else:
                analog = [math.sin(2 * math.pi * 50 * at) + random_.gauss(0, 0.01) for at in times]
                synced = [0] * block
            ai0.send_block(analog)
            sync_line.send_block(synced)
            loop.send_block([level_at(at) for at in times])
            index += block

    return drive


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m openscilab_device.demo", description=__doc__.split("\n\n")[0])
    parser.add_argument("demo", choices=DEMOS)
    parser.add_argument("--server", help="host[:port] of openSciLab (default: found by its beacon)")
    parser.add_argument("--token", default="", help="the token of openSciLab (Settings → Remote devices)")
    parser.add_argument("--name", help="the device's name (default: the demo's name)")
    arguments = parser.parse_args(argv)
    device, drive = build(arguments.demo, server=arguments.server, token=arguments.token)
    if arguments.name:
        device.name = arguments.name
    device.start()
    print(f"{device.name}: connecting to {arguments.server or 'openSciLab (beacon)'} ... Ctrl+C ends it")
    threading.Thread(target=drive, daemon=True).start()
    try:
        while not device._stop.wait(1.0):
            if not device.connected and device.last_error:
                print("  not connected:", device.last_error)
    except KeyboardInterrupt:
        pass
    device.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
