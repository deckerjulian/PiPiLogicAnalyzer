#!/usr/bin/env python3
# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Measures what has to stay fast: decoding, the sync path, and above all how long a thread that
receives samples (a stream, a remote device) has to wait while something else works::

    python tools/benchmark.py                 # everything
    python tools/benchmark.py decode stall    # some of it

``stall``: a probe thread wants to run every millisecond, as the thread of a stream or of a remote
connection does; while a large UART capture is decoded it records how late it ran (the decoder in a
thread of this process - it holds the GIL - and in the decoder process). ``stream``: does a stream
overflow while the application works (pure Python, garbage collection, large numpy work)? First a
device behind a socket with a ring of 128 KiB like the Pico firmware, read by a thread or by a process
of its own; then the simulated Pico (USB and its ring emulated) in the application and in a device
process. The numbers depend on the computer; compare them before and after a change on the same one.
"""

from __future__ import annotations

import os
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np

from openscilab.driver.models import AnalyzerChannel, CaptureSession


def uart_session(samples: int) -> CaptureSession:
    from openscilab.driver.simulated.circuit import make_source

    source = make_source({"type": "uart", "text": "Hello openSciLab, a benchmark! " * 50, "baud": 115200})
    session = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=samples)
    channel = AnalyzerChannel(channel_number=0)
    channel.samples = source.digital(0.0, 1e6, samples)
    session.capture_channels = [channel]
    return session


def uart_provider():
    from openscilab.sigrok.provider import DecoderInstance, SigrokProvider

    provider = SigrokProvider()
    provider.instances = [DecoderInstance("uart", "UART", {0: 0}, {"baudrate": 115200})]
    return provider


# ------------------------------------------------------------------ decode
def bench_decode() -> dict:
    from openscilab.core.capture_io import load_capture
    from openscilab.core.profiles import read_profiles_file
    from openscilab.sigrok.provider import SigrokProvider

    result = {}
    session = uart_session(2_000_000)
    provider = uart_provider()
    provider.registry.load()
    start = time.perf_counter()
    provider.run(session)
    seconds = time.perf_counter() - start
    result["UART, 2 M samples (s)"] = seconds
    result["UART (M samples/s)"] = 2.0 / seconds
    capture = load_capture(os.path.join(ROOT, "examples", "c64-demo.lac"))
    provider = SigrokProvider()
    provider.load_configuration(read_profiles_file(os.path.join(ROOT, "examples", "profiles",
                                                                "c64-expansion-port.json"))[0].decoder_configuration)
    provider.registry.load()
    start = time.perf_counter()
    provider.run(capture.session)
    result["C64 bus, 31 k samples, 2 decoders (s)"] = time.perf_counter() - start
    return result


# -------------------------------------------------------------------- sync
def bench_sync() -> dict:
    from openscilab.core import timing
    from openscilab.driver.remote.server import Received, RemoteConnection
    from openscilab.lab.nodes.remote import pair_edges

    result = {}
    # an analog input recording the sync signal: 1 s at 1 MS/s in blocks of 10 000
    connection = RemoteConnection(None, None, ("", 0))
    connection.description = {"name": "bench", "inputs": [{"name": "sync", "kind": "analog", "rate": 1e6,
                                                           "sync": True}]}
    edges = []
    connection._sync_listeners["sync"] = [edges.extend]
    rng = np.random.default_rng(1)
    level = (np.cumsum(rng.uniform(0.02, 0.06, 40)) * 1e6).astype(int)
    signal = (np.searchsorted(level, np.arange(1_000_000), side="right") % 2) * 3.3 + rng.normal(0, 0.05, 1_000_000)
    start = time.perf_counter()
    for first in range(0, 1_000_000, 10_000):
        block = signal[first:first + 10_000].astype(np.float32)
        connection._sync_block(Received("sync", "analog", 0.0, first / 1e6, samples=block, rate=1e6, index=first))
    result["analog sync input, 1 s at 1 MS/s (s)"] = time.perf_counter() - start
    result[f"  edges found ({int(np.count_nonzero(level < 1_000_000))} in the signal)"] = len(edges)
    # pairing the edges of a recording with those of a device: 256 each
    local = np.cumsum(rng.uniform(0.02, 0.06, 256))
    device = [(float(at) + 0.0003, index % 2, (float(at) + 0.0003, index % 2)) for index, at in enumerate(local)]
    local_edges = [(float(at), index % 2) for index, at in enumerate(local)]
    start = time.perf_counter()
    for _ in range(20):
        pair_edges(local_edges, device, lambda at: at, 0.008, {})
    result["pairing 256 x 256 edges (ms each)"] = (time.perf_counter() - start) / 20 * 1e3
    # an acquisition finding the sample index of a time (timing.align, every edge)
    acquisition = timing.Acquisition(1e6, 0.0, exact_start=0.0)
    for block in range(4096):
        acquisition.given(block * 0.01, block * 10_000)
    start = time.perf_counter()
    for at in np.linspace(0, 40.0, 2000):
        acquisition.index_of(float(at))
    result["index of a time, 4096 blocks (µs each)"] = (time.perf_counter() - start) / 2000 * 1e6
    return result


# ------------------------------------------------------------------- stall
def probe(stop: threading.Event, late: list) -> None:
    """Wants to run every millisecond; records how late it ran."""
    due = time.perf_counter()
    while not stop.is_set():
        due += 0.001
        wait = due - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        late.append(max(time.perf_counter() - due, 0.0))
        due = max(due, time.perf_counter())


def stalls(work) -> dict:
    late: list = []
    stop = threading.Event()
    thread = threading.Thread(target=probe, args=(stop, late), daemon=True)
    thread.start()
    time.sleep(0.2)
    late.clear()
    start = time.perf_counter()
    work()
    seconds = time.perf_counter() - start
    stop.set()
    thread.join()
    late.sort()
    return {"work (s)": seconds, "probe late, median (ms)": statistics.median(late) * 1e3,
            "probe late, 99 % (ms)": late[int(len(late) * 0.99)] * 1e3, "probe late, max (ms)": late[-1] * 1e3}


def bench_stall() -> dict:
    session = uart_session(2_000_000)
    provider = uart_provider()
    provider.registry.load()
    result = {}
    for key, value in stalls(lambda: time.sleep(1.0)).items():
        result[f"idle: {key}"] = value
    worker = threading.Thread(target=provider.run, args=(session,))
    for key, value in stalls(lambda: (worker.start(), worker.join())).items():
        result[f"decoding in a thread: {key}"] = value
    try:
        from openscilab.sigrok import worker as decode_worker
    except ImportError:
        return result
    service = decode_worker.service()
    service.run(provider, uart_session(1000))  # (the process is started and warm)
    for key, value in stalls(lambda: service.run(provider, session)).items():
        result[f"decoding in the decoder process: {key}"] = value
    return result


# ------------------------------------------------------------------ stream
RING = 128 * 1024
CHUNK = 4096


def _device(sock, rate, stop, result) -> None:  # pragma: no cover - in a process of its own
    """Produces ``rate`` bytes per second from a ring of RING bytes; counts how often it ran over."""
    import struct

    sock.setblocking(False)
    payload = struct.pack("<I", CHUNK) + bytes(range(256)) * (CHUNK // 256)
    start = time.perf_counter()
    sent = lost = overflows = 0
    pending = b""
    while not stop.is_set():
        produced = int((time.perf_counter() - start) * rate) - lost
        while produced - sent >= CHUNK or pending:
            pending = pending or payload
            try:
                done = sock.send(pending)
            except BlockingIOError:
                break
            pending = pending[done:]
            if not pending:
                sent += CHUNK
        if produced - sent > RING:
            overflows += 1
            lost += produced - sent  # (the firmware would end the stream; here it goes on, to count)
        time.sleep(0.0002)
    result.put(overflows)


def _reader(sock, total, stop) -> None:  # pragma: no cover - a thread or a process of its own
    """What the Pico driver does: header, chunk, the bits of 8 channels into arrays."""
    import socket
    import struct

    sock.settimeout(0.2)
    arrays = [np.zeros(1 << 20, np.uint8) for _ in range(8)]
    position = 0
    pending = b""
    while not stop.is_set():
        try:
            data = sock.recv(1 << 16)
        except socket.timeout:
            continue
        except OSError:
            return
        if not data:
            return
        pending += data
        while len(pending) >= 4 and len(pending) - 4 >= struct.unpack_from("<I", pending)[0]:
            size = struct.unpack_from("<I", pending)[0]
            raw = np.frombuffer(pending[4:4 + size], dtype=np.uint8)
            pending = pending[4 + size:]
            position = 0 if position + size > len(arrays[0]) else position
            for bit, array in enumerate(arrays):
                array[position:position + size] = (raw >> bit) & 1
            position += size
            total.value += size


def _load_python() -> None:
    end = time.perf_counter() + 2.0
    value = 0
    while time.perf_counter() < end:
        for index in range(10000):
            value += index * index % 7


def _load_gc() -> None:
    import gc

    heap = [[index, str(index)] for index in range(1_500_000)]
    for _ in range(4):
        gc.collect()
        time.sleep(0.2)
    del heap


def _load_numpy() -> None:
    data = np.random.default_rng(1).integers(0, 2, 8_000_000).astype(np.uint8)
    end = time.perf_counter() + 2.0
    while time.perf_counter() < end:
        np.nonzero(np.diff(data))[0].tolist()


LOADS = {"idle": lambda: time.sleep(2.0), "pure Python": _load_python, "garbage collection": _load_gc,
         "numpy": _load_numpy}


def _socket_case(rate: float, reader: str, load) -> int:
    import multiprocessing
    import socket

    context = multiprocessing.get_context("spawn")
    host, device = socket.socketpair()
    for sock in (host, device):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    stop_device, stop_reader, result = context.Event(), context.Event(), context.Queue()
    total = context.Value("q", 0, lock=False)
    if reader == "thread":
        consumer = threading.Thread(target=_reader, args=(host, total, stop_reader), daemon=True)
    else:
        consumer = context.Process(target=_reader, args=(host, total, stop_reader), daemon=True)
    consumer.start()
    producer = context.Process(target=_device, args=(device, rate, stop_device, result), daemon=True)
    time.sleep(0.5)
    producer.start()
    time.sleep(0.5)
    load()
    stop_device.set()
    overflows = result.get(timeout=10)
    stop_reader.set()
    consumer.join(3)
    producer.join(3)
    host.close()
    device.close()
    return overflows


def _pico_case(where: str, load) -> str:
    """A stream of 8 channels at 800 kHz (800 kB/s, what the Pico's firmware reports: the ring lasts
    164 ms) of the simulated Pico while the application works; how it ended."""
    from openscilab.driver.base import ACQUISITION_STREAM, CaptureError
    from openscilab.driver.models import TriggerType

    if where == "application":
        from openscilab.driver.simulated import open_simulated

        instrument = open_simulated("pico")
    else:
        from openscilab.driver.process import open_instrument

        instrument = open_instrument("sim:pico")
    driver = instrument.capture.driver
    session = CaptureSession(frequency=800_000, pre_trigger_samples=0, post_trigger_samples=2_400_000,
                             trigger_type=TriggerType.IMMEDIATE, acquisition_mode=ACQUISITION_STREAM)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in range(8)]
    done = threading.Event()
    results = []
    try:
        assert driver.start_capture(session, lambda args: (results.append(args), done.set())) == CaptureError.NONE
        time.sleep(0.3)
        load()
        driver.stop_capture()
        done.wait(10)
    finally:
        instrument.close()
    if not results:
        return "no answer"
    return "OVERFLOW" if results[0].error and "overflow" in results[0].error.lower() else "ok"


def bench_stream() -> dict:
    result = {}
    for rate in (800_000, 6_000_000):
        for name, load in LOADS.items():
            for reader in ("thread", "process"):
                result[f"socket {rate / 1e6:g} MB/s, reader in a {reader}, {name}: overflows"] = \
                    _socket_case(rate, reader, load)
    for name, load in LOADS.items():
        for where in ("application", "device process"):
            try:
                result[f"sim:pico 3 MB/s in the {where}, {name}"] = _pico_case(where, load)
            except ImportError:
                result[f"sim:pico 3 MB/s in the {where}, {name}"] = "-"
    return result


BENCHMARKS = {"decode": bench_decode, "sync": bench_sync, "stall": bench_stall, "stream": bench_stream}


def main(names: list) -> None:
    print(f"Python {sys.version.split()[0]}, switch interval {sys.getswitchinterval() * 1e3:g} ms")
    for name in names or list(BENCHMARKS):
        print(f"\n{name}")
        for key, value in BENCHMARKS[name]().items():
            print(f"  {key:<70} {value:10.3f}" if isinstance(value, float) else f"  {key:<70} {value:>10}")


if __name__ == "__main__":
    main(sys.argv[1:])
