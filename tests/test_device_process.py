"""Devices in a process of their own (driver/process): the simulated Pico read in a device process
does what it does in the application - captures, streams, GPIO, the monitor, its details - and keeps
streaming while the application is busy, where it overflows in the application. A device process
that dies fails its capture with a message; one whose application went away ends."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from openscilab.core.instrument import GpioFacet, InstrumentStatus, MonitorFacet
from openscilab.driver.base import (
    ACQUISITION_STREAM,
    CaptureError,
    DeviceConnectionError,
    FirmwareOutdatedError,
)
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.process import ProcessEnded, open_instrument

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def outdated(address: str, **_options):  # a factory of the device process (by name)
    raise FirmwareOutdatedError("The firmware is too old.", device="Pico", location="/dev/cu.test")


def session(samples: int, channels=(0, 1), stream: bool = False, rate: int = 200_000) -> CaptureSession:
    capture = CaptureSession(frequency=rate, pre_trigger_samples=0, post_trigger_samples=samples,
                             trigger_type=TriggerType.IMMEDIATE,
                             acquisition_mode=ACQUISITION_STREAM if stream else "buffer")
    capture.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    return capture


def run(driver, capture: CaptureSession, timeout: float = 15.0):
    done = threading.Event()
    results = []
    assert driver.start_capture(capture, lambda args: (results.append(args), done.set())) == CaptureError.NONE
    assert done.wait(timeout)
    return results[0]


@pytest.fixture
def pico():
    instrument = open_instrument("sim:pico")
    yield instrument
    instrument.close()


def test_the_simulated_pico_in_a_device_process(pico):
    driver = pico.capture.driver
    assert pico.process.pid != os.getpid() and pico.process.alive
    assert driver.channel_count == 24 and "GPIO" in driver.capabilities()
    assert driver.get_limits([0, 1], "stream").max_post_samples > 0
    # a capture: the application's own session gets the samples
    capture = session(2000)
    result = run(driver, capture)
    assert result.success and result.session is capture and len(capture.capture_channels[0].samples) == 2000
    assert result.arrived is not None and driver.command_time is not None and not driver.is_capturing
    # a stream: progress names the session, carries when it arrived, the samples are shared
    stream = session(200_000, channels=(0, 1, 2, 3), stream=True)
    progress = []
    handler = progress.append
    driver.add_capture_progress_handler(handler)
    result = run(driver, stream)
    driver.wait_for_events()
    driver.remove_capture_progress_handler(handler)
    assert result.success and len(stream.capture_channels[3].samples) == 200_000
    assert progress and all(args.session is stream and args.arrived is not None for args in progress)
    from openscilab.core import shared_arrays

    assert shared_arrays.describe(stream.capture_channels[0].samples) is not None  # not copied through the pipe
    # the facets live in the device process
    gpio = pico.facet(GpioFacet)
    gpio.write("GP16", 1)
    assert gpio.read("GP16") == 1 and len(pico.pins()) == 26
    states = []
    monitor = pico.facet(MonitorFacet)
    remove = monitor.on_state(states.append)
    monitor.start(100, ["GP12"])
    time.sleep(0.3)
    monitor.stop()
    remove()
    assert states and type(states[0]).__name__ == "MonitorState"
    assert "Process" in dict(pico.details())


def test_a_device_process_that_dies_fails_its_capture(pico):
    driver = pico.capture.driver
    done = threading.Event()
    results = []
    stream = session(10_000_000, stream=True)
    assert driver.start_capture(stream, lambda args: (results.append(args), done.set())) == CaptureError.NONE
    time.sleep(0.3)
    pico.process.process.kill()
    assert done.wait(10)
    assert not results[0].success and "device process ended" in results[0].error
    assert pico.status == InstrumentStatus.DISCONNECTED and not driver.is_capturing
    with pytest.raises(ProcessEnded):
        pico.facet(GpioFacet).read("GP16")


def test_a_device_process_ends_with_its_application(tmp_path):
    script = tmp_path / "app.py"
    script.write_text(
        "import os, sys\n"
        "from openscilab.driver.process import open_instrument\n"
        "if __name__ == '__main__':\n"
        "    instrument = open_instrument('sim:pico')\n"
        "    print(instrument.process.pid, flush=True)\n"
        "    os._exit(0)  # gone without closing anything\n")
    output = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": ROOT})
    pid = int(output.stdout.split()[0])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"the device process {pid} still runs")


def test_errors_of_opening_come_back_as_they_were():
    with pytest.raises(DeviceConnectionError):
        open_instrument("pico:/dev/no-such-port")
    with pytest.raises(FirmwareOutdatedError) as caught:
        open_instrument("pico:/dev/cu.test", factory=f"{__name__}:outdated")
    assert caught.value.device == "Pico" and caught.value.location == "/dev/cu.test"
    assert "too old" in str(caught.value)


def test_a_busy_application_does_not_overflow_a_stream_of_a_device_process():
    """The acceptance of step 4b: the simulated Pico (USB and its 128 KiB ring emulated) at 800 kB/s,
    while the application runs pure Python - in the application its stream overflows."""
    import benchmark

    assert benchmark._pico_case("device process", benchmark.LOADS["pure Python"]) == "ok"
    assert benchmark._pico_case("application", benchmark.LOADS["pure Python"]) == "OVERFLOW"


# --------------------------------------------------------------- where devices open
def test_devices_on_usb_open_in_a_device_process_by_default(monkeypatch):
    from openscilab.core import preferences
    from openscilab.driver import process
    from openscilab.lab.engine import devices

    assert preferences.DEVICE_PROCESS is True  # (the tests switch it off: their fake drivers live here)
    preferences._load()["devices.process"] = True
    assert process.wanted("pico:/dev/cu.usbmodem1") and process.wanted("dslogic:1:4")
    assert process.wanted("arduino:COM5") and process.wanted("pico-multi:a,b") and process.wanted("/dev/ttyACM0")
    assert not process.wanted("sim:pico") and not process.wanted("rigol:192.168.1.20")
    assert not process.wanted("arduino-sim:uno") and not process.wanted("remote:pi")
    opened = []
    monkeypatch.setattr(process, "open_instrument", lambda address, **options: opened.append(address) or "instrument")
    assert devices.open_instrument("pico:/dev/cu.test") == "instrument" and opened == ["pico:/dev/cu.test"]
    monkeypatch.setattr(process, "INSIDE", True)  # (a device process opens its device itself)
    assert not process.wanted("pico:/dev/cu.test")
    preferences._load()["devices.process"] = False
    monkeypatch.setattr(process, "INSIDE", False)
    assert not process.wanted("pico:/dev/cu.test")


def test_the_device_list_opens_by_address(shell, monkeypatch):
    from openscilab.core import preferences
    from openscilab.driver import process
    from openscilab.ui.devices import arduino as arduino_devices
    from openscilab.ui.devices import pico as pico_devices

    preferences._load()["devices.process"] = True
    opened = []

    def open_in_process(address, **options):
        opened.append(address)
        return open_instrument("sim:pico")

    monkeypatch.setattr(process, "open_instrument", open_in_process)
    instrument = shell.connect_entry(pico_devices.serial_entry("/dev/cu.test"))
    try:
        assert opened == ["pico:/dev/cu.test"] and instrument in shell.hub and instrument.process.alive
    finally:
        shell.disconnect_instrument(instrument)
    assert not instrument.process.alive
    backend = arduino_devices.ArduinoBackend()
    entries = {entry.kind: entry for entry in backend.manual_entries()}
    assert backend.address(next(iter(backend.manual_entries())), None).startswith("arduino-sim:")
    assert entries


# ------------------------------------------------------------------- time, flows
def test_a_flow_streams_from_a_device_process_and_places_its_samples(pico):
    import numpy as np

    from openscilab.core import timing
    from openscilab.core.hub import Hub
    from openscilab.lab import yaml_io
    from openscilab.lab.engine import Engine

    hub = Hub()
    hub.add(pico)
    flow = yaml_io.loads("""
flow: Stream
nodes:
  pico: {type: device.instrument, address: "sim:pico"}
  rec: {type: device.stream, channels: [GP12], rate: 200 kHz, duration: 0.4 s}
edges:
  - pico.device -> rec.device
""")
    blocks = []
    before = time.monotonic()
    engine = Engine(flow, mode="real", hub=hub)
    engine.subscribe(lambda event: event.kind == "value" and event.port == "GP12" and blocks.append(event.value))
    assert engine.run(timeout=30).ok
    assert engine.devices["pico"] is pico  # the instrument of the device list, in its device process
    assert sum(len(block) for block in blocks) == 80_000
    acquisition = timing.timing_of(pico).current
    state = acquisition.state()
    # the samples are placed between the start command (stamped in the device process) and their arrival
    assert state.method == timing.METHOD_BOUNDS and state.uncertainty < 0.01
    assert acquisition.clock.started >= before and acquisition.clock.started == pico.capture.driver.command_time
    starts = [block.time.start for block in blocks]
    assert np.all(np.diff(starts) > 0)  # in order, one after the other


# ------------------------------------------------------------------ watchdog
class HangingFacet:
    """A facet of the device process whose call never returns (a driver stuck in its hardware)."""

    title = "Hanging"

    def __init__(self) -> None:
        self.instrument = None

    def hang(self) -> None:
        time.sleep(600)

    def close(self) -> None:
        pass


def hanging(address: str, **_options):  # a factory of the device process (by name)
    from openscilab.core.instrument import Instrument

    instrument = Instrument("Hanging", uri=address)
    instrument.add_facet(HangingFacet())
    return instrument


def test_a_call_the_device_process_never_answers_ends_it(monkeypatch):
    from openscilab.driver.process import proxy

    monkeypatch.setattr(proxy, "CALL_TIMEOUT", 1.5)
    instrument = open_instrument("sim:free", factory=f"{__name__}:hanging")
    try:
        facet = instrument.facet(HangingFacet)
        started = time.monotonic()
        with pytest.raises(ProcessEnded, match="did not answer .*hang.* within 1.5 s"):
            facet.hang()
        assert time.monotonic() - started < 10
        assert instrument.status == InstrumentStatus.DISCONNECTED and not instrument.process.alive
        with pytest.raises(ProcessEnded):
            facet.hang()
    finally:
        instrument.close()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP freezes the process as a hang would")
def test_a_device_process_without_a_sign_of_life_is_ended(monkeypatch):
    import signal

    from openscilab.driver.process import proxy

    monkeypatch.setattr(proxy, "BEAT_TIMEOUT", 2.0)
    instrument = open_instrument("sim:pico")
    try:
        gpio = instrument.facet(GpioFacet)
        assert gpio.read("GP16") in (0, 1, True, False) or gpio.read("GP16") is not None
        time.sleep(1.5)  # (beats arrive: nothing happens)
        assert instrument.process.alive and time.monotonic() - instrument.process.last_beat < 2
        os.kill(instrument.process.pid, signal.SIGSTOP)  # (frozen: no beats, no answers)
        deadline = time.monotonic() + 15
        while instrument.process.alive and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not instrument.process.alive and "stopped answering" in instrument.process.end_reason
        assert instrument.status == InstrumentStatus.DISCONNECTED
        with pytest.raises(ProcessEnded, match="stopped answering"):
            gpio.read("GP16")
    finally:
        instrument.close()
