"""Command line interface, scripting API and device discovery."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading

import numpy as np
import pytest

from pipilogicanalyzer import api, cli
from pipilogicanalyzer.core import capture_io
from pipilogicanalyzer.driver import discovery
from pipilogicanalyzer.driver.base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_IMMEDIATE_TRIGGER,
    CAPABILITY_STREAM_IMMEDIATE_ONLY,
    CAPABILITY_TRIGGER_SEQUENCE,
    AnalyzerDriverBase,
    CaptureCompletedArgs,
    CaptureError,
    DeviceConnectionError,
)
from pipilogicanalyzer.driver.models import (
    AnalyzerChannel,
    CaptureSession,
    ConditionKind,
    TriggerCondition,
    TriggerStage,
    TriggerType,
)

FIXTURE_DECODERS = os.path.join(os.path.dirname(__file__), "fixtures", "decoders")
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def fresh_registries():
    api._registries.clear()
    yield
    api._registries.clear()
    sys.modules.pop("testdec", None)
    sys.modules.pop("testdec.pd", None)
    sys.modules.pop("teststack", None)
    sys.modules.pop("teststack.pd", None)


class FakeDriver(AnalyzerDriverBase):
    """A device with 16 channels: buffer up to 100 MHz, stream up to 10 MHz (immediate only)."""

    def __init__(self, capabilities=(CAPABILITY_IMMEDIATE_TRIGGER, CAPABILITY_STREAM_IMMEDIATE_ONLY), fail=None):
        super().__init__()
        self._capabilities = frozenset(capabilities)
        self.sessions: list[CaptureSession] = []
        self.fail = fail
        self.never_completes = False
        self.stopped = False
        self.disposed = False

    device_version = "FAKE_1"
    max_frequency = 100_000_000
    channel_count = 16
    buffer_size = 64_000

    @property
    def is_capturing(self):
        return False

    def capabilities(self):
        return self._capabilities

    def acquisition_modes(self):
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM)

    def max_frequency_for(self, channels, acquisition_mode=None):
        return 10_000_000 if acquisition_mode == ACQUISITION_STREAM else self.max_frequency

    def start_capture(self, session, completed_handler=None):
        self.sessions.append(session)
        if self.fail == "start":
            return CaptureError.BAD_PARAMS
        if self.never_completes:
            return CaptureError.NONE
        total = session.pre_trigger_samples + session.post_trigger_samples
        for channel in session.capture_channels:
            channel.samples = ((np.arange(total) >> channel.channel_number) & 1).astype(np.uint8)
        args = CaptureCompletedArgs(success=self.fail != "complete", session=session, error="Lost the device")
        threading.Thread(target=self._raise_capture_completed, args=(args, completed_handler)).start()
        return CaptureError.NONE

    def stop_capture(self):
        self.stopped = True
        return True

    def dispose(self):
        self.disposed = True
        super().dispose()


@pytest.fixture
def fake(monkeypatch):
    driver = FakeDriver()
    opened = []

    def open_device(device_id, download_bitstream=False):
        opened.append(device_id)
        return driver

    monkeypatch.setattr(api, "open_device", open_device)
    monkeypatch.setattr(
        api, "list_devices", lambda: [discovery.DeviceInfo("pico:/dev/fake", "Fake board", discovery.KIND_PICO)]
    )
    driver.opened = opened
    return driver


def make_capture_file(path: str) -> str:
    session = CaptureSession(frequency=1000, pre_trigger_samples=10, post_trigger_samples=30)
    clock = ((np.arange(40) // 4) % 2).astype(np.uint8)  # rising edges at 4, 12, 20, 28, 36
    session.capture_channels = [
        AnalyzerChannel(channel_number=2, channel_name="CLK", samples=clock),
        AnalyzerChannel(channel_number=5, samples=1 - clock),
    ]
    capture_io.save_capture(path, session)
    return path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ---------------------------------------------------------------------- parsing
def test_parse_channels():
    assert cli.parse_channels("0-3,9, 2") == [0, 1, 2, 3, 9]
    for text in ("", "a", "5-2"):
        with pytest.raises(cli.CliError):
            cli.parse_channels(text)


def test_parse_trigger():
    assert cli.parse_trigger("edge:3:falling") == api.Edge(3, rising=False)
    assert cli.parse_trigger("edge:0") == api.Edge(0, rising=True)
    assert cli.parse_trigger("pattern:0=1,1=0") == api.Pattern({0: 1, 1: 0})
    assert cli.parse_trigger("fast:4=1") == api.Pattern({4: 1}, fast=True)
    assert cli.parse_trigger("immediate") == api.Immediate()
    assert cli.parse_trigger("simulation:2") == api.Simulation(2)
    assert cli.parse_trigger(None) is None
    for text in ("edge:x", "edge:1:up", "pattern:0", "level:1"):
        with pytest.raises(cli.CliError):
            cli.parse_trigger(text)


def test_parse_decoder_spec():
    assert cli.parse_decoder_spec("i2c:scl=0, sda=1,address_format=unshifted") == (
        "i2c", {"scl": "0", "sda": "1", "address_format": "unshifted"},
    )
    assert cli.parse_decoder_spec("uart") == ("uart", {})
    with pytest.raises(cli.CliError):
        cli.parse_decoder_spec("uart:rx")


def test_parse_rate_and_duration():
    assert api.parse_rate("10M") == 10_000_000
    assert api.parse_rate("100k") == 100_000
    assert api.parse_rate("1e6") == 1_000_000
    assert api.parse_rate("24 MHz") == 24_000_000
    assert api.parse_duration("5ms") == pytest.approx(0.005)
    assert api.parse_duration("250us") == pytest.approx(250e-6)
    assert api.parse_duration("2") == 2.0
    with pytest.raises(ValueError):
        api.parse_rate("fast")
    with pytest.raises(ValueError):
        api.parse_duration("5 parsecs")


# ------------------------------------------------------------------ no Qt GUI
def test_cli_does_not_load_the_user_interface():
    code = (
        "import sys, pipilogicanalyzer.cli, pipilogicanalyzer.api; "
        "pipilogicanalyzer.cli.build_parser(); "
        "bad = [m for m in sys.modules if m.startswith('PySide6.QtWidgets') or m.startswith('pipilogicanalyzer.ui')]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=PROJECT)
    assert result.returncode == 0, result.stdout + result.stderr


# -------------------------------------------------------------------- devices
def test_devices(capsys, fake):
    code, out, _ = run(capsys, "devices")
    assert code == 0
    assert out.strip() == "pico:/dev/fake\tFake board"


def test_no_devices(capsys, monkeypatch):
    monkeypatch.setattr(api, "list_devices", lambda: [])
    code, _, err = run(capsys, "devices")
    assert code == 1 and "No device" in err


def test_info(capsys, fake):
    code, out, _ = run(capsys, "info", "pico:/dev/fake")
    assert code == 0
    assert "Identification: FAKE_1" in out
    assert "Highest rate: 100 MHz" in out
    assert "Modes: buffer, stream" in out
    assert fake.opened == ["pico:/dev/fake"] and fake.disposed


# -------------------------------------------------------------------- capture
def test_capture_with_edge_trigger(capsys, fake, tmp_path):
    output = str(tmp_path / "capture.sr")
    code, out, err = run(
        capsys, "capture", "-d", "pico:/dev/fake", "-c", "0-2,4", "-r", "10M", "-t", "1ms",
        "--pre", "500", "--trigger", "edge:2:falling", "--names", "A,B", "-o", output,
    )
    assert code == 0, err
    session = fake.sessions[0]
    assert session.frequency == 10_000_000
    assert session.pre_trigger_samples == 500
    assert session.post_trigger_samples == 9_500
    assert session.trigger_type == TriggerType.EDGE
    assert session.trigger_channel == 2 and session.trigger_inverted
    assert [c.channel_name for c in session.capture_channels] == ["A", "B", "", ""]
    loaded = api.load(output)
    assert loaded.channel_numbers == [0, 1, 2, 4]
    assert loaded.trigger_sample == 500
    np.testing.assert_array_equal(loaded.samples(4), (np.arange(10_000) >> 4) & 1)
    assert "written to" in out and "Capturing" in err


def test_capture_validation_errors_are_one_line(capsys, fake, tmp_path):
    output = str(tmp_path / "capture.lac")
    code, _, err = run(capsys, "capture", "-c", "0", "-r", "1G", "-n", "1000", "-o", output)
    assert code == 1
    assert err.count("\n") == 1 and "out of range" in err
    code, _, err = run(capsys, "capture", "-c", "0", "-n", "1000", "--mode", "stream", "--trigger", "edge:0", "-o", output)
    assert code == 1 and "start at once" in err
    code, _, err = run(capsys, "capture", "-c", "20", "-o", output)
    assert code == 1 and "does not exist" in err
    code, _, err = run(capsys, "capture", "-c", "0", "-n", "1000000", "-o", output)
    assert code == 1 and "Too many samples" in err
    assert not os.path.exists(output)


def test_capture_and_decode(capsys, fake, tmp_path):
    output = str(tmp_path / "capture.csv")
    annotations = str(tmp_path / "edges.json")
    code, _, err = run(
        capsys, "--decoders-dir", FIXTURE_DECODERS, "capture", "-c", "0-1", "-r", "1M", "-n", "64",
        "--trigger", "immediate", "-o", output, "--decode", "testdec:clk=1,label=up", "--decode", "teststack",
        "-a", annotations,
    )
    assert code == 0, err
    assert fake.sessions[0].trigger_type == TriggerType.IMMEDIATE
    with open(output, encoding="utf-8") as handle:
        assert handle.readline().strip() == "Channel 1,Channel 2"
    with open(annotations, encoding="utf-8") as handle:
        records = json.load(handle)["annotations"]
    # channel 1 rises every 4 samples, from sample 2
    assert [r["start_sample"] for r in records if r["decoder"] == "TestDec"] == list(range(2, 64, 4))
    assert records[0]["value"] == "up 1"
    assert [r["value"] for r in records if r["decoder"] == "TestStack"][:2] == ["edge 1", "edge 2"]


def test_capture_on_the_emulated_device(capsys, tmp_path):
    output = str(tmp_path / "simulated.vcd")
    code, _, err = run(capsys, "capture", "-d", "emulated", "-c", "0-3", "-r", "1M", "-n", "2000", "-q", "-o", output)
    assert code == 0, err
    assert err == ""
    with open(output, encoding="utf-8") as handle:
        assert "$var wire 1" in handle.read()


def test_capture_failures(capsys, fake, tmp_path):
    output = str(tmp_path / "capture.lac")
    fake.fail = "start"
    code, _, err = run(capsys, "capture", "-c", "0", "-n", "100", "-o", output)
    assert code == 1 and "parameters are incorrect" in err
    fake.fail = "complete"
    code, _, err = run(capsys, "capture", "-c", "0", "-n", "100", "-o", output)
    assert code == 1 and "Lost the device" in err


def test_capture_timeout_stops_the_device(capsys, fake, tmp_path):
    fake.never_completes = True
    code, _, err = run(
        capsys, "capture", "-c", "0", "-n", "100", "--timeout", "0.2", "-o", str(tmp_path / "c.lac")
    )
    assert code == 1 and "No trigger" in err
    assert fake.stopped


def test_missing_bitstream_explains_the_download(capsys, monkeypatch, tmp_path):
    from pipilogicanalyzer.driver.dslogic.driver import BitstreamMissingError

    def missing(device_id, download_bitstream=False):
        assert not download_bitstream
        raise BitstreamMissingError("DSLogicU2Pro16.bin", "DSLogic U2Pro16")

    monkeypatch.setattr(api, "open_device", missing)
    code, _, err = run(capsys, "info", "dslogic")
    assert code == 1
    assert "DSLogicU2Pro16.bin" in err and err.count("\n") == 1
    assert "--download-bitstream" in err or "DSView" in err


# ------------------------------------------------------------ convert, decode
@pytest.mark.parametrize("extension", [".sr", ".lac", ".lac.gz"])
def test_convert_round_trip(capsys, tmp_path, extension):
    source = make_capture_file(str(tmp_path / "source.lac"))
    target = str(tmp_path / f"target{extension}")
    code, _, err = run(capsys, "convert", source, target)
    assert code == 0, err
    back = str(tmp_path / "back.lac")
    assert run(capsys, "convert", target, back)[0] == 0
    original, loaded = api.load(source), api.load(back)
    assert loaded.frequency == 1000 and loaded.trigger_sample == 10
    assert loaded.channel_numbers == [2, 5]
    assert [c.display_name for c in loaded.channels] == ["CLK", "Channel 6"]
    for number in (2, 5):
        np.testing.assert_array_equal(loaded.samples(number), original.samples(number))


def test_convert_to_csv_with_time(capsys, tmp_path):
    source = make_capture_file(str(tmp_path / "source.lac"))
    target = str(tmp_path / "out.csv")
    assert run(capsys, "convert", "--time", source, target)[0] == 0
    with open(target, encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["Time", "CLK", "Channel 6"]
    assert rows[1] == ["-0.01", "0", "1"]


def test_convert_errors(capsys, tmp_path):
    source = make_capture_file(str(tmp_path / "source.lac"))
    code, _, err = run(capsys, "convert", source, str(tmp_path / "out.txt"))
    assert code == 1 and "Unknown file type" in err
    code, _, err = run(capsys, "convert", str(tmp_path / "missing.lac"), str(tmp_path / "out.sr"))
    assert code == 1 and err.startswith("pipilogicanalyzer-cli: error:")
    code, _, err = run(capsys, "convert", str(tmp_path / "out.vcd"), str(tmp_path / "out.sr"))
    assert code == 1 and "Unknown file type" in err


def test_decode_file(capsys, tmp_path):
    source = make_capture_file(str(tmp_path / "source.lac"))
    output = str(tmp_path / "edges.csv")
    code, out, err = run(
        capsys, "--decoders-dir", FIXTURE_DECODERS, "decode", source, "-D", "testdec:clk=CLK,mode=first", "-o", output
    )
    assert code == 0, err
    assert "2 annotations" in out
    with open(output, encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[1] == ["-0.006", "-0.005", "4", "5", "TestDec", "Edges", "edge 1"]
    assert rows[2][-2:] == ["Infos", "first only"]

    code, out, _ = run(capsys, "--decoders-dir", FIXTURE_DECODERS, "decode", source, "-D", "testdec:clk=2")
    assert code == 0
    assert out.splitlines()[0].split("\t") == ["-0.006000000", "TestDec", "Edges", "edge 1"]


def test_decode_errors(capsys, tmp_path):
    source = make_capture_file(str(tmp_path / "source.lac"))
    base = ["--decoders-dir", FIXTURE_DECODERS, "decode", source, "-D"]
    for spec, message in (
        ("nosuchdecoder", "not installed"),
        ("testdec", "Assign the channels clk"),
        ("testdec:clk=7", "no channel 7"),
        ("testdec:clk=2,speed=3", "no option 'speed'"),
        ("testdec:clk=2,mode=last", "Invalid value 'last'"),
    ):
        code, _, err = run(capsys, *base, spec)
        assert code == 1 and message in err, (spec, err)


def test_decoders_list(capsys):
    code, out, _ = run(capsys, "--decoders-dir", FIXTURE_DECODERS, "decoders")
    assert code == 0
    lines = {line.split("\t")[0]: line for line in out.splitlines()}
    assert lines["testdec"].endswith("\tclk,aux")
    assert "(on testdec)" in lines["teststack"]


# ------------------------------------------------------------------------ api
def test_api_capture_settings(fake):
    with api.open() as device:
        assert fake.opened == ["pico:/dev/fake"]
        capture = device.capture(channels=[0, 1], rate="1M", samples=1000, pre_trigger=100,
                                 trigger=api.Pattern({3: 1, 4: 0, 5: 1}))
    session = fake.sessions[0]
    assert session.trigger_type == TriggerType.COMPLEX
    assert (session.trigger_channel, session.trigger_bit_count, session.trigger_pattern) == (3, 3, 0b101)
    assert capture.frequency == 1_000_000 and capture.sample_count == 1000
    assert capture.samples("Channel 2").tolist()[:4] == [0, 0, 1, 1]
    assert capture.times()[100] == 0.0
    assert capture.duration == pytest.approx(0.001)
    assert fake.disposed


def test_api_defaults_and_errors(fake):
    device = api.Device(fake)
    session = device.build_session(channels=[0])
    assert session.frequency == 100_000_000
    assert session.trigger_type == TriggerType.IMMEDIATE and session.pre_trigger_samples == 0
    assert device.build_session(channels=[0], mode="stream").frequency == 10_000_000

    with pytest.raises(ValueError, match="consecutive"):
        device.build_session(trigger=api.Pattern({0: 1, 2: 0}))
    with pytest.raises(ValueError, match="at most 5"):
        device.build_session(trigger=api.Pattern({n: 1 for n in range(6)}, fast=True))
    with pytest.raises(ValueError, match="either"):
        device.build_session(samples=10, duration="1ms")
    with pytest.raises(ValueError, match="before the trigger"):
        device.build_session(trigger=api.Edge(0), pre_trigger=1_000_000)
    with pytest.raises(ValueError, match="twice"):
        device.build_session(channels=[1, 1])
    with pytest.raises(ValueError, match="sequences"):
        device.build_session(trigger=api.Sequence([TriggerStage()]))
    with pytest.raises(ValueError, match="threshold"):
        device.build_session(threshold=1.5)
    with pytest.raises(ValueError, match="mode"):
        device.build_session(mode="burst")


def test_api_sequence_trigger():
    driver = FakeDriver(capabilities=(f"{CAPABILITY_TRIGGER_SEQUENCE}=2", "TRIGGER_CONDITIONS=edge/pattern"))
    device = api.Device(driver)
    stages = [TriggerStage(TriggerCondition(ConditionKind.EDGE, channel=1)), TriggerStage()]
    session = device.build_session(trigger=api.Sequence(stages))
    assert session.trigger_type == TriggerType.SEQUENCE
    assert session.trigger_sequence.stages == stages
    with pytest.raises(ValueError, match="at most 2"):
        device.build_session(trigger=api.Sequence(stages * 2))
    with pytest.raises(ValueError, match="pulse"):
        device.build_session(trigger=api.Sequence([TriggerStage(TriggerCondition(ConditionKind.PULSE))]))
    with pytest.raises(ValueError, match="without a trigger"):
        device.build_session()


def test_api_emulated_device_needs_simulation():
    device = api.open("emulated")
    with pytest.raises(ValueError, match="Simulation"):
        device.build_session(trigger=api.Edge(0))
    capture = device.capture(channels=[0, 1], rate="1M", samples=500)
    assert capture.sample_count == 500


def test_api_decode_stacks_automatically(tmp_path):
    capture = api.load(make_capture_file(str(tmp_path / "source.lac")))
    registry = api.decoder_registry((FIXTURE_DECODERS,))
    records = capture.decode("teststack", channels={"clk": "CLK"}, registry=registry)
    assert [r.decoder for r in records].count("TestStack") == 5
    assert [r.value for r in records if r.decoder == "TestStack"][0] == "edge 1"
    records = capture.decode("testdec", channels={0: 2}, options={"label": "rise", "skip": "3"}, registry=registry)
    assert records[0].value == "rise 1"


def test_api_save_and_load(tmp_path):
    capture = api.load(make_capture_file(str(tmp_path / "source.lac")))
    for name in ("copy.lac.gz", "copy.sr", "copy.vcd", "copy.csv"):
        capture.save(str(tmp_path / name))
        assert os.path.getsize(tmp_path / name) > 0
    with pytest.raises(ValueError, match="Unknown file type"):
        api.load(str(tmp_path / "copy.vcd"))
    with pytest.raises(KeyError):
        capture.samples(7)


# ------------------------------------------------------------------ discovery
def test_list_devices(monkeypatch):
    from pipilogicanalyzer.driver.dslogic import usb
    from pipilogicanalyzer.driver.pico import detector

    monkeypatch.setattr(detector, "detect", lambda: [detector.DetectedDevice("/dev/cu.usbmodem1", "E661")])
    monkeypatch.setattr(usb, "list_devices", lambda: [usb.UsbDeviceInfo(1, 4, 0x0030, "DSLogic U2Pro16")])
    assert discovery.list_devices() == [
        discovery.DeviceInfo("pico:/dev/cu.usbmodem1", "PiPiLogicAnalyzer on /dev/cu.usbmodem1, S/N E661", "pico"),
        discovery.DeviceInfo("dslogic:1:4", "DSLogic U2Pro16 (USB 2, 1:4)", "dslogic"),
    ]


def test_open_device_identifiers(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "_open_pico", lambda text: calls.append(("pico", text)))
    monkeypatch.setattr(discovery, "_open_dslogic", lambda text, download: calls.append(("dslogic", text, download)))

    from pipilogicanalyzer.driver.pico import multi

    monkeypatch.setattr(multi, "MultiAnalyzerDriver", lambda strings: calls.append(("multi", strings)))

    discovery.open_device("pico:/dev/cu.usbmodem1")
    discovery.open_device("/dev/ttyACM0")
    discovery.open_device("COM5")
    discovery.open_device("pico-net:192.168.1.5:4045")
    discovery.open_device("192.168.1.6:4045")
    discovery.open_device("pico-multi:/dev/a,pico:/dev/b")
    discovery.open_device("/dev/a, /dev/b,10.0.0.1:4045")
    discovery.open_device("dslogic:1:4", download_bitstream=True)
    discovery.open_device("dslogic")
    assert calls == [
        ("pico", "/dev/cu.usbmodem1"),
        ("pico", "/dev/ttyACM0"),
        ("pico", "COM5"),
        ("pico", "192.168.1.5:4045"),
        ("pico", "192.168.1.6:4045"),
        ("multi", ["/dev/a", "/dev/b"]),
        ("multi", ["/dev/a", "/dev/b", "10.0.0.1:4045"]),
        ("dslogic", "1:4", True),
        ("dslogic", "", False),
    ]
    with pytest.raises(DeviceConnectionError):
        discovery.open_device("")
    assert discovery.open_device("emulated").driver_type.value == "Emulated"


def test_open_missing_dslogic():
    with pytest.raises(DeviceConnectionError, match="No DSLogic"):
        discovery.open_device("dslogic")
    with pytest.raises(DeviceConnectionError, match="1:9"):
        discovery.open_device("dslogic:1:9")


def test_dslogic_bitstream_download(monkeypatch):
    from pipilogicanalyzer.driver.dslogic import driver as dslogic_driver
    from pipilogicanalyzer.driver.dslogic import resources, usb

    info = usb.UsbDeviceInfo(1, 4, 0x0030, "DSLogic U2Pro16")
    monkeypatch.setattr(usb, "find_device", lambda location: info)
    monkeypatch.setattr(dslogic_driver, "prepare", lambda found: found)
    attempts = []

    def driver(found):
        attempts.append(found)
        if len(attempts) == 1:
            raise dslogic_driver.BitstreamMissingError("X.bin", "DSLogic U2Pro16")
        return "driver"

    monkeypatch.setattr(dslogic_driver, "DSLogicDriver", driver)
    monkeypatch.setattr(resources, "downloadable", lambda name: True)
    downloads = []
    monkeypatch.setattr(resources, "download", lambda name: downloads.append(name))

    with pytest.raises(dslogic_driver.BitstreamMissingError):
        discovery.open_device("dslogic:1:4")
    attempts.clear()
    assert discovery.open_device("dslogic:1:4", download_bitstream=True) == "driver"
    assert downloads == ["X.bin"]
