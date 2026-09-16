"""Aligning the boards of a multi device capture (reference line and clock estimate)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from pipilogicanalyzer.core import alignment
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession

PERIOD = 20  # samples per clock cycle (a C64 cycle at 20 MHz)
CYCLES = 2000
COUNT = PERIOD * CYCLES
BUS_DELAY = 1  # the address changes one sample after the falling clock edge


def true_signals(seed: int = 7):
    """Clock with falling edges at 10, 30, ... and 16 address lines changing after them."""
    clock = np.zeros(COUNT, dtype=np.uint8)
    for start in range(0, COUNT, PERIOD):
        clock[start:start + PERIOD // 2] = 1
    rng = np.random.default_rng(seed)
    values = rng.integers(0, 1 << 16, size=CYCLES + 1)
    address = np.zeros((16, COUNT), dtype=np.uint8)
    for cycle in range(CYCLES + 1):
        first = max(cycle * PERIOD - PERIOD // 2 + BUS_DELAY, 0)
        last = min(first + PERIOD, COUNT)
        if first >= COUNT:
            break
        for bit in range(16):
            address[bit, first:last] = (values[cycle] >> bit) & 1
    return clock, address


def moved(samples: np.ndarray, offset: float, drift: float = 0.0) -> np.ndarray:
    """What a board sees whose samples are ``offset`` (+ drift) samples late."""
    positions = np.arange(samples.size)
    return samples[np.clip(positions - np.rint(offset + drift * positions).astype(np.int64), 0, samples.size - 1)]


def capture(offset: float, drift: float = 0.0, reference: bool = True, reference_signal=None) -> tuple:
    clock, address = true_signals()
    master = [AnalyzerChannel(channel_number=0, channel_name="Φ2 (E)", samples=clock.copy())]
    if reference:
        signal = address[0].copy() if reference_signal is None else reference_signal
        master.append(AnalyzerChannel(channel_number=14, channel_name="A0 (Y) ref", samples=signal))
    slave = [
        AnalyzerChannel(channel_number=24 + bit, channel_name=f"A{bit} ({pin})", samples=moved(address[bit], offset, drift))
        for bit, pin in enumerate("YXWVUTSRPNMLKJHF")
    ]
    session = CaptureSession(frequency=20_000_000, pre_trigger_samples=0, post_trigger_samples=COUNT)
    session.capture_channels = master + slave
    return session, address


def mismatch(session: CaptureSession, address: np.ndarray) -> float:
    slave = [channel for channel in session.capture_channels if channel.channel_number >= 24]
    inner = slice(4 * alignment.MAX_SHIFT, COUNT - 4 * alignment.MAX_SHIFT)
    wrong = sum(np.count_nonzero(channel.samples[inner] != address[bit][inner]) for bit, channel in enumerate(slave))
    return wrong / (16 * (inner.stop - inner.start))


@pytest.mark.parametrize("offset", [-3, 2])
def test_a_reference_line_gives_the_exact_offset(offset):
    session, address = capture(offset)

    (result,) = alignment.align_devices(session, 24)

    assert result.method == "reference" and result.source == "A0 (Y) ref"
    assert round(result.offset) == -offset and result.changed
    assert mismatch(session, address) == 0


def test_a_reference_line_corrects_clock_drift():
    session, address = capture(offset=1, drift=4 / COUNT)  # 4 samples over the capture

    (result,) = alignment.align_devices(session, 24)

    assert result.method == "reference"
    assert -5.5 < result.offset + result.drift * COUNT < -3.5
    assert mismatch(session, address) < 0.01


def test_aligned_boards_are_left_alone():
    session, address = capture(offset=0)
    before = [channel.samples.copy() for channel in session.capture_channels]

    (result,) = alignment.align_devices(session, 24)

    assert not result.changed and "already aligned" in result.describe()
    assert all(np.array_equal(old, channel.samples) for old, channel in zip(before, session.capture_channels))


def test_without_a_reference_the_clock_removes_changes_before_the_edge():
    session, _address = capture(offset=-3, reference=False)  # the bus changes 2 samples before the edge

    (result,) = alignment.align_devices(session, 24)

    assert result.method == "clock" and result.source == "Φ2 (E)"
    assert round(result.offset) in (2, 3)
    edges = alignment.falling_edges(session.capture_channels[0].samples)
    changes = np.concatenate([alignment.transitions(channel.samples) for channel in session.capture_channels[1:]])
    assert alignment._setup_violations(edges, np.sort(changes)) == 0


def test_an_unconnected_reference_line_falls_back_to_the_clock():
    floating = np.zeros(COUNT, dtype=np.uint8)  # the wire is missing: the input stays low
    session, _address = capture(offset=-3, reference_signal=floating)

    (result,) = alignment.align_devices(session, 24)

    assert result.method == "clock"


def test_single_board_captures_are_not_touched():
    session, _address = capture(offset=2)
    session.capture_channels = [channel for channel in session.capture_channels if channel.channel_number < 24]
    assert alignment.align_devices(session, 24) == []
    assert alignment.channels_per_device_of(session) is None


def test_every_available_method_is_offered():
    session, _address = capture(offset=-3)

    (options,) = alignment.alignment_options(session, 24).values()

    assert [option.method for option in options] == ["reference", "clock"]
    assert round(options[0].offset) == 3


def test_the_align_dialog_offers_the_methods_and_leaving_unchanged():
    from PySide6.QtWidgets import QApplication

    from pipilogicanalyzer.ui.dialogs.align_dialog import AlignDialog

    QApplication.instance() or QApplication([])
    session, _address = capture(offset=-3)
    options = alignment.alignment_options(session, 24)
    dialog = AlignDialog(options)

    texts = [button.text() for button in dialog._groups[1].buttons()]
    assert texts[0].startswith("Reference line A0 (Y) ref (exact): move by +3 samples")
    assert texts[1].startswith("Clock Φ2 (E) (estimate)")
    assert texts[2] == "Leave unchanged"
    assert dialog.choices[1] is options[1][0]
    dialog.select(1, 1)
    assert dialog.choices[1] is options[1][1]
    dialog.select(1, 2)
    assert dialog.choices[1] is None


class Chooser:
    """Stands in for the align dialog and picks option ``index`` for every board."""

    index = 0
    seen: list = []

    def __init__(self, options, parent=None):
        self.options = options
        Chooser.seen.append(options)

    def exec(self):
        return True

    @property
    def choices(self):
        return {device: candidates[self.index] for device, candidates in self.options.items()}


def test_the_menu_action_lets_the_user_choose_the_method(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pipilogicanalyzer.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    infos = []
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.info", lambda *args, **kwargs: infos.append(args))
    monkeypatch.setattr("pipilogicanalyzer.ui.main_window.AlignDialog", Chooser)
    session, address = capture(offset=2)
    window = MainWindow()
    try:
        window.load_session(session)
        Chooser.index = 0
        window.align_boards()
        assert "board 2 moved by -2 samples (reference A0 (Y) ref)" in window.statusBar().currentMessage()
        assert "reference A0 (Y) ref" in infos[-1][3]
        assert "Alignment" in window.info_label.text()
        assert mismatch(window.model.session, address) == 0

        # Choosing another method starts again from the samples as captured.
        Chooser.index = 1
        window.align_boards()
        assert "clock Φ2 (E)" in window.statusBar().currentMessage()
        assert [option.method for option in Chooser.seen[-1][1]] == ["reference", "clock"]
        assert round(Chooser.seen[-1][1][0].offset) == -2

        single, _address = capture(offset=0)
        single.capture_channels = single.capture_channels[:1]
        window.load_session(single)
        window.align_boards()
        assert infos[-1][2] == "The boards could not be aligned."
    finally:
        window.close()
