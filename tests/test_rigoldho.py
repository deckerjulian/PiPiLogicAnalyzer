"""The Rigol DHO900 driver against the simulated oscilloscope (``rigol-sim:direct`` and
``rigol-sim:bridge``): SCPI parsing, captures directly and through the bridge app, triggers,
stopping, thresholds, the generator, the beacon and the probe tool."""

from __future__ import annotations

import socket
import threading
import time

import numpy as np
import pytest

from openscilab.core import waveform as waves
from openscilab.core.instrument import Instrument, InstrumentError
from openscilab.driver import discovery
from openscilab.driver.base import CaptureError, DeviceConnectionError
from openscilab.driver.models import AnalogChannel, AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.rigoldho import beacon, scpi
from openscilab.driver.simulated.scpi_shell import open_shell


@pytest.fixture(scope="module")
def direct():
    driver = open_shell("direct")
    yield driver
    driver.dispose()


@pytest.fixture
def bridge():
    driver = open_shell("bridge")
    yield driver
    driver.dispose()


def make_session(channels=(0, 1, 2, 3), analog=(), pre=1000, post=9000, rate=1_000_000) -> CaptureSession:
    session = CaptureSession(frequency=rate, pre_trigger_samples=pre, post_trigger_samples=post)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    session.analog_channels = [AnalogChannel(channel_number=number) for number in analog]
    session.trigger_type = TriggerType.IMMEDIATE
    return session


def run(driver, session, seconds=15.0):
    results = []
    done = threading.Event()
    assert driver.start_capture(session, lambda args: (results.append(args), done.set())) == CaptureError.NONE
    assert done.wait(seconds)
    return results[0]


# ------------------------------------------------------------------ parsing
def test_blocks_preamble_meta():
    assert scpi.block(b"abc") == b"#13abc"
    assert scpi.parse_block(b"#210" + bytes(10) + b"\n") == bytes(10)
    preamble = scpi.parse_preamble("0,2,1000,1,1.0e-06,-1.0e-04,0,0.04,0,128")
    assert preamble["points"] == 1000 and preamble["xorig"] == -1e-4 and preamble["yref"] == 128
    assert scpi.parse_meta("points=10;rate=1e6;CH1.yinc=0.04") == {"points": "10", "rate": "1e6", "CH1.yinc": "0.04"}
    assert scpi.command("la_channel", n=3, value="ON") == ":LA:DIGital3:DISPlay ON"


def test_a_server_that_is_no_rigol_is_refused():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def answer():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                while True:
                    data = connection.recv(1024)
                    if not data:
                        break
                    if b"*IDN?" in data:
                        connection.sendall(b"ACME,THING,1,2\n")

    threading.Thread(target=answer, daemon=True).start()
    with pytest.raises(DeviceConnectionError, match="no Rigol"):
        discovery.open_device(f"rigol:127.0.0.1:{server.getsockname()[1]}")
    server.close()


# ------------------------------------------------------------------ direct
def test_the_instrument_describes_itself(direct):
    assert direct.device_version.startswith("Rigol DHO924S")
    assert not direct.uses_bridge and "PROGRESSIVE" not in direct.capabilities()
    assert {"THRESHOLD", "ANALOG=4", "PATTERN_GROUPS=0-15", "AFG"} <= direct.capabilities()
    assert direct.analog_channel_names() == ["CH1", "CH2", "CH3", "CH4"]
    assert direct.memory_depth(16, 4) == 10_000_000


def test_a_direct_capture_digital_and_analog(direct):
    session = make_session(analog=(0,))
    session.threshold_voltage = 1.4
    result = run(direct, session)
    assert result.success, result.error
    assert len(session.capture_channels[0].samples) == 10_000
    assert session.pre_trigger_samples == 1000 and session.post_trigger_samples == 9000
    assert np.all(np.diff(session.capture_channels[0].samples[:10].astype(int)) != 0)  # D0 toggles every sample
    volts = session.analog_channels[0].raw * session.analog_channels[0].scale + session.analog_channels[0].offset
    assert volts.max() - volts.min() > 0.5 and -5 <= volts.min() and volts.max() <= 5  # the 1 kHz sine
    assert direct.shell.thresholds == {1: 1.4, 2: 1.4}


def test_an_edge_trigger(direct):
    session = make_session(channels=(3,), pre=200, post=800)
    session.trigger_type = TriggerType.EDGE
    session.trigger_channel = 3
    result = run(direct, session)
    assert result.success
    samples = session.capture_channels[0].samples
    assert samples[199] == 0 and samples[200] == 1


def test_settings_beyond_the_instrument_are_refused(direct):
    assert direct.start_capture(make_session(post=40_000_000)) == CaptureError.BAD_PARAMS
    assert direct.start_capture(make_session(channels=(16,))) == CaptureError.BAD_PARAMS
    session = make_session()
    session.trigger_type = TriggerType.SEQUENCE
    assert direct.start_capture(session) == CaptureError.UNSUPPORTED


def test_a_capture_without_trigger_can_be_stopped(direct):
    session = make_session(channels=(0,), pre=10, post=990, rate=1000)
    session.trigger_type = TriggerType.EDGE
    session.trigger_channel = 15
    results = []
    assert direct.start_capture(session, results.append) == CaptureError.NONE
    time.sleep(0.2)
    assert direct.stop_capture()
    time.sleep(0.2)
    assert results == [] and not direct.is_capturing


# ------------------------------------------------------------------ bridge
def test_a_bridge_capture_arrives_progressively(bridge):
    from openscilab.driver.rigoldho import fetcher

    assert bridge.uses_bridge and "PROGRESSIVE" in bridge.capabilities()
    original = fetcher.TILE_SAMPLES
    fetcher.TILE_SAMPLES = 4096
    try:
        tiles = []
        tile_done = threading.Event()
        bridge.add_capture_tile_handler(lambda args: (tiles.append(args), args.complete and tile_done.set()))
        session = make_session(analog=(1,), pre=2000, post=18000)
        result = run(bridge, session)
        assert result.success and session.progressive is not None
        overview = session.progressive.overviews[("d", 0)]
        low, high = overview.envelope(0, 20000, 10)
        assert low.min() == 0 and high.max() == 1  # shown at once from the overview
        session.progressive.prioritize(16000, 17000)
        assert tile_done.wait(15)
        # 20000 samples asked for: the smallest memory depth that holds them is 100k points
        assert session.pre_trigger_samples + session.post_trigger_samples == 100_000
        assert session.frequency == 1_000_000 and session.pre_trigger_samples == 2000
        assert session.progressive.complete and len(tiles) == session.progressive.tile_count
        assert np.all(np.diff(session.capture_channels[0].samples[:20].astype(int)) != 0)  # D0 toggles
        volts = session.analog_channels[0].raw * session.analog_channels[0].scale + session.analog_channels[0].offset
        assert 1.0 < volts.max() - volts.min() < 10.0  # the 10 kHz square of CH2
        assert bridge.connection.query(":BRIDge:CACHe:LIST?").startswith("1,")
    finally:
        fetcher.TILE_SAMPLES = original


def test_an_interrupted_transfer_resumes(bridge):
    from openscilab.driver.rigoldho import fetcher

    original = fetcher.TILE_SAMPLES
    fetcher.TILE_SAMPLES = 1000
    try:
        session = make_session(channels=(0,), pre=0, post=20000)
        stops = []

        def stop_after_two(args):
            stops.append(args)
            if len(stops) == 2:
                bridge.stop_transfer()

        bridge.add_capture_tile_handler(stop_after_two)
        assert run(bridge, session).success
        deadline = time.monotonic() + 5
        while (len(stops) < 2 or bridge._fetcher._thread.is_alive()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not session.progressive.complete and session.progressive.interrupted
        bridge.remove_capture_tile_handler(stop_after_two)
        assert bridge.resume_transfer(session)
        deadline = time.monotonic() + 10
        while not session.progressive.complete and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.progressive.complete
    finally:
        fetcher.TILE_SAMPLES = original


# ---------------------------------------------------------------- generator
def test_the_generator_drives_ch1(bridge):
    instrument = Instrument.from_driver(bridge, uri=bridge.address)
    generator = instrument.generator
    assert [output.name for output in generator.outputs()] == ["GI"]
    generator.start("GI", waves.standard("square", 5000.0, amplitude=2.0, offset=0.5, duty=0.25))
    shell = bridge.shell
    assert shell.instrument.generator.running("GI")
    played = shell.instrument.generator._running["GI"][0]
    assert (played.kind, played.frequency, played.amplitude, played.offset, played.duty) == ("square", 5000.0, 2.0, 0.5, 0.25)
    generator.start("GI", waves.standard("sine", 1000.0, modulation="sweep", sweep_start=100, sweep_stop=2000))
    assert shell.instrument.generator._running["GI"][0].modulation == "sweep"
    generator.start("GI", waves.from_points([0.0, 1.0, -1.0, 0.5], frequency=200.0))
    assert shell.instrument.generator._running["GI"][0].points.tolist() == [0.0, 1.0, -1.0, 0.5]
    generator.stop("GI")
    assert not shell.instrument.generator.running("GI")
    with pytest.raises(InstrumentError):
        generator.start("GI", waves.standard("sine", 50e6))


# ----------------------------------------------------------- beacon, probe
def test_beacons():
    item = beacon.parse("OPENSCILAB_BRIDGE 0.1.0 5560 DHO924S DHO9A123", "192.168.1.20")
    assert (item.host, item.port, item.model, item.serial) == ("192.168.1.20", 5560, "DHO924S", "DHO9A123")
    assert "DHO924S at 192.168.1.20" in item.label
    assert beacon.parse("SOMETHING ELSE", "x") is None


def test_the_probe_tool(direct, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "tools" / "dho_la_probe.py"
    spec = importlib.util.spec_from_file_location("dho_la_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "QUERIES", ["*IDN?", ":TRIGger:STATus?", ":ACQuire:SRATe?"])
    monkeypatch.setattr(module, "SOURCES", ["D0", "CHANnel1"])
    assert run(direct, make_session(analog=(0,))).success
    port = direct.shell.port
    assert module.main(["127.0.0.1", "--port", str(port), "--points", "1000", "--speed",
                        "--speed-bytes", "20000", "--out", str(tmp_path)]) == 0
    folder = next(tmp_path.iterdir())
    assert (folder / "D0.bin").stat().st_size == 1000 and (folder / "probe.json").exists()
    assert "DHO924S" in (folder / "probe.json").read_text()


def test_discovery_and_the_device_list():
    from openscilab.ui.devices.rigoldho import RigolBackend

    labels = [entry.label for entry in RigolBackend().manual_entries()]
    assert "Rigol oscilloscope by address..." in labels and any("with bridge app" in label for label in labels)
    with pytest.raises(DeviceConnectionError):
        discovery.open_device("rigol-sim:nothing")
    driver = discovery.open_device("rigol-sim:bridge")
    try:
        assert driver.address == "rigol-sim:bridge"
    finally:
        driver.dispose()


def test_the_device_card_lists_the_bridge_cache(shell):
    driver = open_shell("bridge")
    instrument = Instrument.from_driver(driver, uri=driver.address)
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    tabs = [card.tabs.tabText(index) for index in range(card.tabs.count())]
    assert "Cache" in tabs
    assert run(driver, make_session(channels=(0,), pre=0, post=1000)).success
    card.cache_panel.refresh()
    assert card.cache_panel.table.rowCount() == 1 and card.cache_panel.table.item(0, 2).text() == "1,000"
    card.cache_panel.table.selectRow(0)
    card.cache_panel.delete_selected()
    assert card.cache_panel.table.rowCount() == 0
    direct_driver = open_shell("direct")
    try:
        assert Instrument.from_driver(direct_driver).facet(type(card.cache_panel.cache).__mro__[1]) is None
    finally:
        direct_driver.dispose()
