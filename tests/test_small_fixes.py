"""Smaller corrections of the review of the code base: the command line, quantities, patterns."""

from __future__ import annotations

import os

import numpy as np
import pytest

from openscilab import api
from openscilab.core import state_mode
from openscilab.core.signals import Analog
from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.sdl import parser
from openscilab.sdl.parser import SDLError, TokenizedSDL

from test_cli import fake, run  # noqa: F401 - fixture and helper


# ------------------------------------------------------------ the command line
def test_what_cannot_be_written_is_refused_before_the_capture(capsys, fake):  # noqa: F811
    captures = []
    driver_class = type(fake) if not isinstance(fake, type) else fake
    start = getattr(driver_class, "start_capture", None)
    if start is not None:
        def counted(self, *args, **kwargs):
            captures.append(1)
            return start(self, *args, **kwargs)

        driver_class.start_capture = counted
    try:
        code, _, err = run(capsys, "capture", "-c", "0", "-n", "100", "-o", "capture.txt")
        assert code == 1 and err.count("\n") == 1 and ".txt" in err
        code, _, err = run(capsys, "capture", "-c", "0", "-n", "100", "-o", "capture.lac", "--annotations", "a.csv")
        assert code == 1 and "--annotations needs" in err
        assert captures == []  # nothing was captured and then thrown away
        assert not os.path.exists("capture.lac")
    finally:
        if start is not None:
            driver_class.start_capture = start


def test_errors_of_files_are_one_line(capsys, tmp_path):
    archive = tmp_path / "broken.sr"
    archive.write_bytes(b"this is no zip archive")
    code, _, err = run(capsys, "convert", str(archive), str(tmp_path / "out.csv"))
    assert code == 1 and err.count("\n") == 1 and "Traceback" not in err

    flow = tmp_path / "broken.flow.yaml"
    flow.write_text("flow: x\nnodes: [unclosed\n")
    code, _, err = run(capsys, "run", str(flow), "--fast")
    assert code == 1 and err.count("\n") == 1 and "Traceback" not in err


def test_durations_are_read_like_every_quantity():
    assert api.parse_duration("5m") == pytest.approx(0.005)  # milli, as in flows and `run --duration`
    assert api.parse_duration("2 min") == 120.0
    assert api.parse_duration("250 us") == pytest.approx(250e-6)
    assert api.parse_duration(0.25) == 0.25
    with pytest.raises(ValueError):
        api.parse_duration("5 V")


# ------------------------------------------------------------------- signals
def reference_to_digital(values, threshold, hysteresis, initial):
    """The loop the conversion with hysteresis was: the state changes above the upper and
    below the lower threshold."""
    high, low = threshold + hysteresis / 2, threshold - hysteresis / 2
    result, state = [], initial
    for value in values:
        if value >= high:
            state = 1
        elif value <= low:
            state = 0
        result.append(state)
    return np.array(result, dtype=np.uint8)


def test_hysteresis_without_a_loop_gives_the_same():
    rng = np.random.default_rng(11)
    for _ in range(50):
        values = rng.normal(1.6, 1.0, int(rng.integers(0, 500)))
        for initial in (0, 1):
            signal = Analog(name="a", values=values)
            digital = signal.to_digital(1.65, hysteresis=0.8, initial=initial)
            assert np.array_equal(digital.values, reference_to_digital(values, 1.65, 0.8, initial))
    noisy = Analog(name="n", values=np.random.default_rng(1).normal(1.65, 2.0, 2_000_000))
    import time

    started = time.monotonic()
    noisy.to_digital(1.65, hysteresis=0.5)
    assert time.monotonic() - started < 0.5  # (it took most of a second)


def test_a_state_capture_without_times_says_so():
    capture = CaptureSession(frequency=1000, post_trigger_samples=10)
    capture.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.zeros(10, np.uint8))]
    with pytest.raises(ValueError, match="no state times"):
        state_mode.states_to_timing(capture)


# ------------------------------------------------------------------ patterns
def test_two_slashes_in_a_string_are_no_comment():
    tokens = parser.get_tokens('$0;{<0>,l1}1;{<1>,h1}1;s"a//b"; // a real comment\nh1;')
    sources = [token.source for token in tokens]
    assert any("a//b" in source for source in sources)
    assert not any("comment" in source for source in sources)
    assert parser.get_tokens("$0; /* a // b */ h2;")[-1].source == "h2"  # a block comment with slashes


def test_escapes_are_read_in_one_pass():
    assert parser._unescape(r"a\\nb") == "a\\nb"  # a backslash and an n, not a line break
    assert parser._unescape(r"a\nb") == "a\nb"
    assert parser._unescape(r'say \"hi\"\; ok\,') == 'say "hi"; ok,'
    assert parser._unescape(r"\q") == r"\q"  # unknown: left as it is


def test_a_pattern_cannot_grow_without_end(monkeypatch):
    monkeypatch.setattr(parser, "MAX_PATTERN_SAMPLES", 10_000)
    with pytest.raises(SDLError, match="longer than"):
        TokenizedSDL("$0;{l1,h1,}0xFFFFFF;").to_samples()
    assert TokenizedSDL("$0;{l1,h1,}100;").to_samples().size == 200
