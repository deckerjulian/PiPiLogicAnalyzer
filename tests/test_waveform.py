"""The waveform model: standard shapes, arbitrary points, patterns, protocol blocks, modulation,
limits of outputs and files."""

from __future__ import annotations

import numpy as np
import pytest

from openscilab.core import signals
from openscilab.core import waveform as waves
from openscilab.core.instrument import OutputInfo
from openscilab.driver.models import AnalyzerChannel, CaptureSession

AFG = OutputInfo("GI", "analog", voltage_range=(-5.0, 5.0), max_points=16384, max_frequency=25e6,
                 capabilities=frozenset({"SWEEP", "BURST", "ARB"}))
PATTERN = OutputInfo("P", "pattern", max_rate=25e6, max_points=1000, pins=("D0", "D1", "D2"))


@pytest.mark.parametrize("kind, expected", [
    (waves.SINE, [0.5, 2.5, 0.5, -1.5]),
    (waves.SQUARE, [2.5, 2.5, -1.5, -1.5]),
    (waves.TRIANGLE, [-1.5, 0.5, 2.5, 0.5]),
    (waves.RAMP, [-1.5, -0.5, 0.5, 1.5]),
    (waves.PULSE, [2.5, 2.5, 0.5, 0.5]),
    (waves.DC, [0.5, 0.5, 0.5, 0.5]),
])
def test_standard_shapes(kind, expected):
    wave = waves.standard(kind, "1 kHz", "2 V", "0.5 V")
    assert wave.analog([0, 0.00025, 0.0005, 0.00075]) == pytest.approx(expected, abs=1e-9)


def test_noise_is_repeatable_and_has_its_amplitude():
    wave = waves.standard(waves.NOISE, 1000, "0.1 V")
    values = wave.samples(1e6, 100_000)
    assert np.std(values) == pytest.approx(0.1, rel=0.05)
    assert np.array_equal(values, wave.samples(1e6, 100_000))


def test_a_formula_gives_one_pass():
    wave = waves.from_formula("2*sin(2*pi*x) + 0.5", frequency="100 Hz", count=400)
    assert len(wave.points) == 400 and wave.voltage_range() == pytest.approx((-1.5, 2.5), abs=1e-3)
    assert wave.analog([0.0025]) == pytest.approx([2.5], abs=1e-3)  # a quarter of 10 ms
    with pytest.raises(waves.WaveformError, match="formula"):
        waves.from_formula("sin(")
    with pytest.raises(waves.WaveformError):
        waves.from_formula("__import__('os')")  # no builtins


def test_csv_and_a_capture_as_points(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text("time,volts\n0,0\n0.001,1\n0.002,2\n0.003,3\n")
    wave = waves.from_csv(str(path))
    assert list(wave.points) == [0, 1, 2, 3] and wave.frequency == pytest.approx(250)  # 4 points at 1 kHz
    analog = signals.Analog(name="CH1", values=[0.0, 1.0], time=signals.TimeBase.uniform(1000))
    assert waves.from_analog(analog).frequency == pytest.approx(500)


def test_sweep_and_burst():
    sweep = waves.standard(waves.SINE, 0, 1, modulation=waves.SWEEP, sweep_start=100, sweep_stop=1100, sweep_time=1)
    # instantaneous frequency: zero crossings get closer
    values = sweep.samples(100_000, 100_000)
    crossings = np.flatnonzero(np.diff(np.signbit(values)))
    assert np.diff(crossings)[0] > 3 * np.diff(crossings)[-1]
    burst = waves.standard(waves.SINE, 1000, 1, modulation=waves.BURST, burst_cycles=2, burst_period=0.01)
    values = burst.samples(100_000, 1000)
    assert np.abs(values[:200]).max() > 0.9 and np.abs(values[250:1000]).max() == 0


def test_sdl_patterns_round_trip():
    wave = waves.pattern_from_sdl({"D0": "l5;h3;l2;", "D1": "h10;"}, "2 MHz")
    assert wave.pattern_length == 10 and list(wave.tracks["D0"]) == [0] * 5 + [1] * 3 + [0] * 2
    assert waves.levels_to_sdl(wave.tracks["D0"]) == "l5;h3;l2;"
    levels = wave.digital(np.arange(25) / 2e6)["D0"]
    assert list(levels[10:20]) == list(wave.tracks["D0"])  # repeats
    once = waves.pattern_from_sdl({"D0": "l2;h2;"}, repeat=1)
    assert list(once.samples(1e6, 8)["D0"]) == [0, 0, 1, 1, 1, 1, 1, 1]  # holds the end
    with pytest.raises(waves.WaveformError, match="D0"):
        waves.pattern_from_sdl({"D0": "x9;"})


def test_a_pattern_from_a_capture():
    session = CaptureSession(frequency=1000, pre_trigger_samples=0, post_trigger_samples=4)
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="A", samples=np.array([0, 1, 1, 0]))]
    wave = waves.pattern_from_capture(session)
    assert wave.rate == 1000 and list(wave.tracks["A"]) == [0, 1, 1, 0]


def test_protocol_blocks():
    uart = waves.uart_tracks("A", baud=1000, rate=10_000, pin="TX", idle=1)["TX"]
    bits = uart[5::10]  # middle of each bit
    assert list(bits) == [1, 0] + [1, 0, 0, 0, 0, 0, 1, 0] + [1, 1]  # 0x41 LSB first
    spi = waves.spi_tracks([0xA5], clock=1000, rate=10_000)
    rising = np.flatnonzero(np.diff(spi["SCK"].astype(int)) == 1) + 1
    assert [int(spi["MOSI"][index]) for index in rising] == [1, 0, 1, 0, 0, 1, 0, 1]
    assert spi["CS"][rising].max() == 0
    i2c = waves.i2c_tracks(0x50, [0x12], clock=1000, rate=8000)
    assert i2c["SDA"][0] == 1 and i2c["SCL"][-1] == 1 and i2c["SDA"][-1] == 1
    both = waves.combine(waves.uart_tracks("x", 1000, 10_000, pin="D0"), {"D1": np.ones(5, np.uint8)})
    assert len(both["D0"]) == len(both["D1"]) and both["D1"][0] == 0 and both["D1"][-1] == 1


def test_limits_of_outputs():
    assert waves.problems(waves.standard(waves.SINE, "1 MHz", "2 V"), AFG) == []
    assert "above" in waves.problems(waves.standard(waves.SINE, "30 MHz", 1), AFG)[0]
    assert "outside" in waves.problems(waves.standard(waves.SINE, 1000, "6 V"), AFG)[0]
    assert "patterns" in waves.problems(waves.standard(waves.SINE, 1000, 1), PATTERN)[0]
    pattern = waves.pattern({"D0": np.zeros(2000), "D7": np.zeros(10)}, "50 MHz")
    found = waves.problems(pattern, PATTERN)
    assert len(found) == 3 and "D7" in found[2]
    square = OutputInfo("PWM", "square", max_frequency=100_000)
    assert waves.problems(waves.standard(waves.SINE, 100, 1, 1), square) == ["PWM makes square waves only (PWM)"]
    many = waves.from_points(np.zeros(20_000))
    assert "points" in waves.problems(many, AFG)[0]
    assert len(waves.fit_points(many, 16384).points) == 16384


def test_files(tmp_path):
    for wave in (waves.standard(waves.SQUARE, "2 kHz", "1.5 V", duty=0.25, modulation=waves.BURST, burst_cycles=3),
                 waves.from_formula("sin(2*pi*x)**3", 50, 256),
                 waves.from_points([0.0, 1.0, 0.5]),
                 waves.pattern_from_sdl({"D0": "l3;h1;", "D2": "h4;"}, "5 MHz", repeat=2)):
        path = tmp_path / "w.wave.yaml"
        waves.save(wave, str(path))
        loaded = waves.load(str(path))
        times = np.arange(200) / 1e5
        if wave.is_pattern:
            assert loaded.rate == wave.rate and loaded.repeat == 2
            assert all(np.array_equal(loaded.tracks[pin], wave.tracks[pin]) for pin in wave.tracks)
        else:
            assert loaded.analog(times) == pytest.approx(wave.analog(times))
    (tmp_path / "p.sdl").write_text("l2;h2;")
    assert list(waves.load(str(tmp_path / "p.sdl")).tracks["D0"]) == [0, 0, 1, 1]


# ------------------------------------------------------------------ document
def test_building_a_pattern_in_the_document_and_capturing_it(shell):
    """Acceptance: an SDL pattern from the waveform document on the simulated pattern generator,
    captured and compared: identical."""
    import threading

    from openscilab.core.compare import compare_sessions
    from openscilab.driver.models import TriggerType
    from openscilab.driver.simulated import open_simulated

    logic = open_simulated("free", fast=True)
    shell.hub.add(logic)
    document = shell.new_waveform()
    document.kind_box.setCurrentIndex(document.kind_box.findData(waves.PATTERN))
    document.rate.setValue(2_000_000)
    document.tracks_edit.setPlainText("D0: l5;h3;l2;h10;l4;h6;\nD3: {l1,h1,}15;\n// a marker at the start\nD7: h1;l29;")
    document.rebuild()
    assert document.waveform.pattern_length == 30 and not document.problems()
    assert document.output_box.currentText().startswith(logic.name)
    assert not document.preview.grab().isNull()
    assert document.start() and document.playing is not None

    session = CaptureSession(frequency=2_000_000, pre_trigger_samples=0, post_trigger_samples=120,
                             trigger_type=TriggerType.EDGE, trigger_channel=7)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in (0, 3, 7)]
    done = threading.Event()
    logic.capture.driver.start_capture(session, lambda args: done.set())
    assert done.wait(10)
    expected = document.waveform.samples(2e6, 120)
    reference = CaptureSession(frequency=2_000_000, pre_trigger_samples=0, post_trigger_samples=120)
    reference.capture_channels = [AnalyzerChannel(channel_number=int(pin[1:]), samples=expected[pin])
                                  for pin in ("D0", "D3", "D7")]
    assert compare_sessions(reference, session).differing_samples == 0
    document.stop()
    assert document.playing is None


def test_the_document_explains_limits_and_saves(shell, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from openscilab.driver.simulated import open_simulated

    shell.hub.add(open_simulated("dho924s", fast=True))
    document = shell.new_waveform()
    document.frequency.setValue(30e6)
    document.rebuild()
    assert "above" in document.problems_label.text() and not document.start_button.isEnabled()
    document.frequency.setValue(1000)
    document.modulation.setCurrentIndex(document.modulation.findData(waves.SWEEP))
    document.rebuild()
    assert not document.problems() and document.dirty
    path = str(tmp_path / "sweep")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (path, ""))
    assert document.save()
    assert document.path == path + ".wave.yaml" and not document.dirty
    reopened = shell.open_file(document.path)
    assert reopened is document  # already open
    shell.area.close_document(document, force=True)
    reopened = shell.open_file(path + ".wave.yaml")
    assert reopened.waveform.modulation == waves.SWEEP and reopened.modulation.currentData() == waves.SWEEP


def test_a_fast_pattern_is_limited_to_the_output(shell):
    from openscilab.driver.simulated import open_simulated

    shell.hub.add(open_simulated("free", fast=True))
    document = shell.new_waveform(waves.pattern({"D0": [0, 1] * 50}, "100 MHz"))
    assert not document.limit_button.isHidden() and "above" in document.problems_label.text()
    assert document.limit_rate()
    assert document.waveform.rate == pytest.approx(25e6) and not document.problems()


def test_play_a_capture_as_signal(shell, make_dataview, monkeypatch):
    """Acceptance: an analog channel of a capture on the simulated AFG, digital channels on the
    pattern generator."""
    from openscilab.driver.simulated import open_simulated
    from openscilab.ui import messages

    shell.hub.add(open_simulated("dho924s", fast=True))
    window = make_dataview()
    window.play_requested.connect(shell.new_waveform)
    session = CaptureSession(frequency=100_000, pre_trigger_samples=0, post_trigger_samples=1000)
    session.capture_channels = [AnalyzerChannel(channel_number=0, channel_name="D0",
                                                samples=(np.arange(1000) // 50 % 2).astype(np.uint8))]
    from openscilab.driver.models import AnalogChannel

    session.analog_channels = [AnalogChannel.from_volts(np.sin(np.arange(1000) / 1000 * 2 * np.pi),
                                                        channel_name="CH1")]
    window.load_session(session)
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 1)  # the analog channel
    window.play_as_signal()
    document = shell.active_document()
    assert document.document_kind == "waveform" and document.waveform.kind == waves.ARBITRARY
    assert len(document.waveform.points) == 1000 and document.waveform.frequency == pytest.approx(100)
    assert "GI" in document.output_box.currentText() and not document.problems()
    assert document.start()
    document.stop()

    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 0)
    window.model.set_cursor("A", 100)
    window.model.set_cursor("B", 299)
    window.play_as_signal()
    pattern_document = shell.active_document()
    assert pattern_document.waveform.is_pattern and pattern_document.waveform.pattern_length == 200
