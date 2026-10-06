"""A NI DAQ (USB-6211 or similar) as a remote device of openSciLab - a template to start from.

The computer the DAQ is plugged into runs this script (Windows or Linux with NI-DAQmx and
``pip install nidaqmx``; copy the folder ``openscilab_device`` next to it)::

    python ni_daq.py --server 192.168.1.20 --token <the token openSciLab shows> --device Dev1

The DAQ appears in openSciLab's device list as "ni-daq" with:

* ``ai0`` - the measurement (analog, 10 kS/s, blocks of 100 samples);
* ``sync`` - AI1 records openSciLab's sync signal (node timing.sync on a GPIO of an instrument,
  wired to AI1 and to a channel of your logic analyzer; node remote.sync with sync: sync). It is
  sampled by the same clock as AI0, so AI0 is aligned to a sample (100 µs at 10 kS/s);
* ``loop`` - AI2, wired to the digital output P0.0: the command ``calibrate_latency`` switches P0.0
  and measures how late the blocks arrive. The latency is kept in ``ni_daq_latency.json`` and used
  from the next start on - without the sync signal the blocks are then still stamped to a fraction
  of a millisecond;
* ``do0`` - P0.0 as an output openSciLab can set.

How the time of the samples is found (docs/timing.md): every block is stamped from when it arrives
(the lower envelope of the arrivals), bounded by the start of the task and less the measured
latency; openSciLab measures the clock of this computer over the network and, with the sync signal,
aligns the samples to the logic analyzer. ``--shared-clock`` when the DAQ's sample clock comes from
openSciLab's instrument (PFI0 as the external sample clock): then only the offset is fitted.
``--ptp`` when this computer's clock follows PTP (linuxptp: ptp4l + phc2sys, and openSciLab's
computer too: Settings → Time).

Without NI-DAQmx the DAQ is simulated, so the script runs on any computer.
"""

import argparse
import math
import random
import threading
import time

from openscilab_device import Device
from openscilab_device.timing import timescale_clock

RATE = 10_000.0
BLOCK = 100
LATENCY_FILE = "ni_daq_latency.json"

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType
    from nidaqmx.stream_readers import AnalogMultiChannelReader
except ImportError:  # no NI-DAQmx: pretend
    nidaqmx = None


class Hardware:
    """The DAQ: three analog inputs in one task (one sample clock), a digital output."""

    def __init__(self, device: str, shared_clock: bool) -> None:
        self.levels = [(0.0, 0)]  # simulated: (time, level) of P0.0
        if nidaqmx is None:
            return
        import numpy as np

        self.task = nidaqmx.Task()
        self.task.ai_channels.add_ai_voltage_chan(f"{device}/ai0:2")
        self.task.timing.cfg_samp_clk_timing(RATE, source=f"/{device}/PFI0" if shared_clock else "",
                                             sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=int(RATE))
        self.reader = AnalogMultiChannelReader(self.task.in_stream)
        self.buffer = np.zeros((3, BLOCK))
        self.output = nidaqmx.Task()
        self.output.do_channels.add_do_chan(f"{device}/port0/line0")

    def start(self) -> None:
        if nidaqmx is not None:
            self.task.start()
        else:
            self.started = time.monotonic()
            self.read = 0

    def read_block(self):
        """The next block of the three inputs (waits until it is there)."""
        if nidaqmx is not None:
            self.reader.read_many_sample(self.buffer, number_of_samples_per_channel=BLOCK, timeout=10.0)
            return self.buffer[0], self.buffer[1], self.buffer[2]
        # simulated: the block arrives 1-2 ms after its last sample
        first = self.started + self.read / RATE
        time.sleep(max(first + BLOCK / RATE + random.uniform(0.001, 0.002) - time.monotonic(), 0.0))
        self.read += BLOCK
        times = [first + n / RATE for n in range(BLOCK)]
        looped = [5.0 * max((item for item in self.levels if item[0] <= at), default=(0.0, 0))[1] for at in times]
        return [math.sin(2 * math.pi * 50 * at) for at in times], [0.0] * BLOCK, looped

    def write(self, level: int) -> None:
        if nidaqmx is not None:
            self.output.write(bool(level))
        else:  # simulated: the command reaches the box half a millisecond later
            self.levels = (self.levels + [(time.monotonic() + 0.0005, int(level))])[-64:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", help="host[:port] of openSciLab (default: found by its beacon)")
    parser.add_argument("--token", default="")
    parser.add_argument("--device", default="Dev1", help="the NI-DAQmx device name")
    parser.add_argument("--shared-clock", action="store_true", help="the sample clock comes from PFI0")
    parser.add_argument("--ptp", action="store_true", help="this computer's clock follows PTP (TAI)")
    arguments = parser.parse_args()

    options = {"clock": timescale_clock("tai"), "timescale": "tai", "timescale_accuracy": 50e-6} if arguments.ptp else {}
    dev = Device("ni-daq", server=arguments.server, token=arguments.token,
                 description="NI DAQ: AI0 measures, AI1 records the sync signal, P0.0 is wired to AI2", **options)
    shared = "shared" if arguments.shared_clock else None
    ai0 = dev.input("ai0", kind="analog", rate=RATE, unit="V", range=(-10, 10), clock=shared, latency=LATENCY_FILE)
    sync = dev.input("sync", kind="analog", rate=RATE, unit="V", range=(0, 5), sync=True, clock=shared,
                     latency=LATENCY_FILE, description="AI1: openSciLab's sync signal")
    loop = dev.input("loop", kind="analog", rate=RATE, unit="V", range=(0, 5), latency=LATENCY_FILE,
                     description="AI2: P0.0 wired back")
    do0 = dev.output("do0", kind="bool", default=False, description="P0.0")
    hardware = Hardware(arguments.device, arguments.shared_clock)
    do0.on_set(lambda value, at: hardware.write(int(bool(value))))

    @dev.command(description="Measures the latency of the inputs with the loopback P0.0 -> AI2 and keeps it.")
    def calibrate_latency(repeats: int = 10) -> dict:
        latency = loop.calibrate(hardware.write, repeats=int(repeats), threshold=2.5, store=LATENCY_FILE)
        for port in (ai0, sync):
            port.latency = latency  # one task, one USB pipe: the same latency
        return {"latency": latency.value, "uncertainty": latency.uncertainty}

    def acquire() -> None:
        for port in (ai0, sync, loop):
            port.start()  # right before the task starts: no sample is older
        hardware.start()
        while True:
            measured, synced, looped = hardware.read_block()
            ai0.send_block(measured)
            sync.send_block(synced)
            loop.send_block(looped)

    dev.start()
    threading.Thread(target=acquire, daemon=True).start()
    print(f"ni-daq: connecting to {arguments.server or 'openSciLab (beacon)'} ... Ctrl+C ends it")
    try:
        while True:
            time.sleep(1.0)
            if not dev.connected and dev.last_error:
                print("  not connected:", dev.last_error)
    except KeyboardInterrupt:
        pass
    dev.stop()


if __name__ == "__main__":
    main()
