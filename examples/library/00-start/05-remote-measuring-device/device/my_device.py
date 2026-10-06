"""The script for the other computer: a measuring device of openSciLab.

Copy this file and the folder ``openscilab_device`` (from openSciLab) to the computer the hardware
is connected to, switch remote devices on in openSciLab (Settings → Remote devices) and run::

    python3 my_device.py --server <address of openSciLab> --token <the token openSciLab shows>

It then appears in openSciLab's device list as "my-device" (address ``remote:my-device``) with the
same inputs, outputs and command as the simulated stand-in of the project (remote-sim:daq):

* ``ai0`` - an analog input, sent in blocks of samples with the time of the first one;
* ``do0`` - a digital output;
* ``calibrate_latency`` - measures how late the blocks arrive with a loopback from ``do0`` to the
  digital input ``loop`` (wire them);
* ``SYNC`` - a sync output: wire it to a channel of a logic analyzer for microsecond alignment
  (node remote.sync), else the clock is measured over the network.

Fill in your hardware in the functions marked HARDWARE; without it the script makes up a 50 Hz sine,
so it runs on any computer.
"""

import argparse
import math
import time

from openscilab_device import Device

RATE = 1000.0  # samples per second of ai0
BLOCK = 100  # samples per block


def read_ai0(count: int, start: float) -> list:
    """HARDWARE: ``count`` samples of the analog input, the first at ``start`` (time.monotonic)."""
    return [math.sin(2 * math.pi * 50 * (start + index / RATE)) for index in range(count)]


def read_loop(count: int, start: float, level: int) -> list:
    """HARDWARE: ``count`` samples of the digital input wired to do0."""
    return [level] * count


def write_do0(level: int) -> None:
    """HARDWARE: set the digital output."""


def write_sync(level: int) -> None:
    """HARDWARE: set the pin of the sync output."""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", help="host[:port] of openSciLab (default: found in the local network)")
    parser.add_argument("--token", default="")
    arguments = parser.parse_args()

    dev = Device("my-device", server=arguments.server, token=arguments.token,
                 description="A measuring device: an analog input, a digital output and a loopback")
    ai0 = dev.input("ai0", kind="analog", rate=RATE, unit="V", description="analog input")
    loop = dev.input("loop", kind="digital", rate=RATE, description="digital input, wired to do0")
    do0 = dev.output("do0", kind="bool", default=False, description="digital output")
    dev.sync_output("SYNC", write_sync, description="wire it to a channel of the logic analyzer")
    level = [0]

    def set_output(value) -> None:
        level[0] = int(bool(value))
        write_do0(level[0])

    do0.on_set(lambda value, at: set_output(value))

    @dev.command(description="Measures the latency of the inputs with the loopback do0 -> loop; returns it.")
    def calibrate_latency(repeats: int = 10) -> dict:
        latency = loop.calibrate(set_output, repeats=int(repeats), interval=0.03)
        ai0.latency = latency  # one converter, one link: the same latency
        return {"latency": latency.value, "uncertainty": latency.uncertainty}

    for port in (ai0, loop):
        port.start()  # right before the acquisition starts
    dev.start()
    print("my-device: connecting ... Ctrl+C ends it")
    start = time.monotonic()
    index = 0
    try:
        while True:
            first = start + index / RATE
            time.sleep(max(first + BLOCK / RATE - time.monotonic(), 0.0))
            ai0.send_block(read_ai0(BLOCK, first))
            loop.send_block(read_loop(BLOCK, first, level[0]))
            index += BLOCK
    except KeyboardInterrupt:
        pass
    finally:
        dev.stop()


if __name__ == "__main__":
    main()
