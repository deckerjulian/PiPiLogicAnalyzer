"""Multi device sets: which board evaluates the trigger and how the other boards follow."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pipilogicanalyzer.driver import multi
from pipilogicanalyzer.driver.base import (
    CAPABILITY_EDGE_TRIGGER_OUT,
    COMPLEX_TRIGGER_DELAY,
    DEFAULT_PATTERN_GROUPS,
    CaptureError,
    CaptureLimits,
    parse_pattern_groups,
)
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType

#: Capabilities of the firmware of this project on a Pico board.
NEW_FIRMWARE = (CAPABILITY_EDGE_TRIGGER_OUT, "PATTERN_GROUPS=0-20/21-23")


class FakeDevice:
    """Stands in for ``PiPiLogicAnalyzerDriver`` and records the capture requests."""

    def __init__(self, name: str, capabilities, started: list) -> None:
        self.name = name
        self.device_version = "LOGIC_ANALYZER_PICO_V7_0"
        self.channel_count = 24
        self.max_frequency = 100_000_000
        self.min_frequency = 3052
        self.buffer_size = 131072
        self.tag = None
        self._capabilities = frozenset(capabilities)
        self._started = started

    def capabilities(self):
        return self._capabilities

    def pattern_trigger_groups(self):
        return parse_pattern_groups(self._capabilities) or DEFAULT_PATTERN_GROUPS

    def get_limits(self, channels):
        return CaptureLimits(min_pre_samples=2, max_pre_samples=10_000, min_post_samples=2, max_post_samples=30_000)

    def start_capture(self, session, handler=None):
        self._started.append((self.name, session))
        return CaptureError.NONE

    def stop_capture(self):
        return True

    def dispose(self):
        pass


@pytest.fixture
def make_set(monkeypatch):
    def build(*capabilities):
        started: list = []
        devices = iter(FakeDevice(f"board{index + 1}", caps, started) for index, caps in enumerate(capabilities))
        monkeypatch.setattr(multi, "PiPiLogicAnalyzerDriver", lambda _connection: next(devices))
        driver = multi.MultiAnalyzerDriver([f"/dev/{index}" for index in range(len(capabilities))])
        driver.started = started  # type: ignore[attr-defined]
        return driver

    return build


def capture(channels, **trigger) -> CaptureSession:
    session = CaptureSession(frequency=100_000_000, pre_trigger_samples=100, post_trigger_samples=1000)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    for name, value in trigger.items():
        setattr(session, name, value)
    return session


def started(driver):
    return [(name, session.trigger_type, session.trigger_channel, session.channel_numbers,
             session.pre_trigger_samples, session.trigger_inverted) for name, session in driver.started]


def test_a_pattern_on_the_second_board_triggers_the_first(make_set):
    driver = make_set(NEW_FIRMWARE, NEW_FIRMWARE)
    session = capture([0, *range(24, 40)], trigger_type=TriggerType.COMPLEX, trigger_channel=24,
                      trigger_bit_count=16, trigger_pattern=0xFFFC)

    assert driver.start_capture(session) is CaptureError.NONE
    # 5 clock cycles of the 100 MHz board at 100 MHz sampling: 5 samples (not 5 ns / 10 ns period)
    offset = round(COMPLEX_TRIGGER_DELAY + 0.3)
    # The following board is armed first, on its trigger input
    assert started(driver) == [
        ("board1", TriggerType.EDGE, 24, [0], 100 + offset, False),
        ("board2", TriggerType.COMPLEX, 0, list(range(16)), 100, False),
    ]
    assert driver.started[1][1].trigger_bit_count == 16


def test_the_first_board_still_triggers_with_older_firmware(make_set):
    driver = make_set((), ())
    session = capture([0, 1, 30], trigger_type=TriggerType.FAST, trigger_channel=1, trigger_bit_count=3)

    assert driver.start_capture(session) is CaptureError.NONE
    assert [(name, kind, channel) for name, kind, channel, *_ in started(driver)] == [
        ("board2", TriggerType.EDGE, 24),
        ("board1", TriggerType.FAST, 1),
    ]


@pytest.mark.parametrize(
    "capabilities,first_channel,bits",
    [
        (NEW_FIRMWARE, 22, 4),  # channels 23 to 26 span both boards
        (NEW_FIRMWARE, 19, 4),  # channels 20 to 23 are not consecutive GPIOs
        ((), 40, 2),  # older firmware: channels 1 to 16 of a board only
        (NEW_FIRMWARE, 24, 17),  # more than 16 bits
    ],
)
def test_patterns_must_fit_into_the_trigger_inputs_of_one_board(make_set, capabilities, first_channel, bits):
    driver = make_set(capabilities, capabilities)
    session = capture([0, 24, 40], trigger_type=TriggerType.COMPLEX, trigger_channel=first_channel,
                      trigger_bit_count=bits)
    assert driver.start_capture(session) is CaptureError.BAD_PARAMS
    assert driver.started == []


def test_an_edge_on_a_channel_drives_the_trigger_output(make_set):
    driver = make_set(NEW_FIRMWARE, NEW_FIRMWARE)
    session = capture([0, 30], trigger_type=TriggerType.EDGE, trigger_channel=30, trigger_inverted=True)

    assert driver.start_capture(session) is CaptureError.NONE
    assert started(driver) == [
        # EDGE_OUT_TRIGGER_DELAY: 3 cycles of the 100 MHz board are 3 samples at 100 MHz
        ("board1", TriggerType.EDGE, 24, [0], 103, False),
        ("board2", TriggerType.EDGE_OUT, 6, [6], 100, True),
    ]


def test_an_edge_on_a_channel_needs_the_new_firmware_on_that_board(make_set):
    driver = make_set(NEW_FIRMWARE, ())
    assert driver.edge_trigger_channels() == list(range(24))

    session = capture([0, 30], trigger_type=TriggerType.EDGE, trigger_channel=30)
    assert driver.start_capture(session) is CaptureError.UNSUPPORTED

    session.trigger_channel = 5
    assert driver.start_capture(session) is CaptureError.NONE


def test_the_external_trigger_starts_every_board_on_its_input(make_set):
    driver = make_set((), ())
    session = capture([0, 30], trigger_type=TriggerType.EDGE, trigger_channel=48, trigger_inverted=True)

    assert driver.start_capture(session) is CaptureError.NONE
    assert started(driver) == [
        ("board1", TriggerType.EDGE, 24, [0], 100, True),
        ("board2", TriggerType.EDGE, 24, [6], 100, True),
    ]


def test_the_trigger_board_has_to_capture(make_set):
    driver = make_set(NEW_FIRMWARE, NEW_FIRMWARE)
    session = capture([0], trigger_type=TriggerType.EDGE, trigger_channel=30)
    assert driver.start_capture(session) is CaptureError.BAD_PARAMS


def test_blast_bursts_and_simulation_are_rejected(make_set):
    driver = make_set(NEW_FIRMWARE, NEW_FIRMWARE)
    assert driver.start_capture(capture([0], trigger_type=TriggerType.BLAST)) is CaptureError.BAD_PARAMS
    assert driver.start_capture(capture([0], trigger_type=TriggerType.SIMULATION)) is CaptureError.UNSUPPORTED
    assert driver.start_capture(capture([0], loop_count=2)) is CaptureError.BAD_PARAMS


def test_pattern_groups_of_the_set(make_set):
    driver = make_set(NEW_FIRMWARE, ())
    assert driver.pattern_trigger_groups() == ((0, 21), (21, 3), (24, 16))


def test_the_capture_dialog_offers_every_trigger_of_the_set(make_set, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pipilogicanalyzer.core import settings
    from pipilogicanalyzer.ui.dialogs.capture_dialog import CaptureDialog

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(settings, "get_settings", lambda *args, **kwargs: None)
    dialog = CaptureDialog(make_set(NEW_FIRMWARE, ()))
    try:
        assert dialog.edge_radio.isEnabled() and not dialog.burst_box.isEnabled()
        box = dialog.trigger_channel_box
        items = [(box.itemText(index), box.itemData(index)) for index in range(box.count())]
        assert items[0] == ("Channel 1 (board 1)", 0)
        assert items[-1] == ("External trigger (every board)", 48)
        assert len(items) == 25  # board 2 has no edge trigger output
        assert dialog.pattern_base_box.maximum() == 48

        def select(*channels):
            for selector in dialog.channel_selectors:
                selector.enabled = selector.channel_number in channels

        select(0, 24)
        dialog.pattern_radio.setChecked(True)
        dialog.pattern_base_box.setValue(25)
        dialog.pattern_edit.setText("0011111111111111")
        session = dialog.build_session()
        assert session is not None
        assert (session.trigger_type, session.trigger_channel, session.trigger_bit_count) == (
            TriggerType.COMPLEX, 24, 16)

        dialog.pattern_base_box.setValue(23)
        dialog.pattern_edit.setText("0101")
        assert dialog.build_session() is None
        assert "25–40" in dialog.validation_label.text()

        select(0)
        dialog.pattern_base_box.setValue(25)
        dialog.pattern_edit.setText("1")
        assert dialog.build_session() is None
        assert "Board 2" in dialog.validation_label.text()

        dialog.edge_radio.setChecked(True)
        box.setCurrentIndex(box.count() - 1)
        session = dialog.build_session()
        assert session is not None and session.trigger_type is TriggerType.EDGE
        assert session.trigger_channel == 48
    finally:
        dialog.close()


def test_the_trigger_offset_scales_with_the_sample_rate(make_set):
    driver = make_set(NEW_FIRMWARE, NEW_FIRMWARE)
    session = capture([0, *range(24, 40)], trigger_type=TriggerType.COMPLEX, trigger_channel=24,
                      trigger_bit_count=16, trigger_pattern=0xFFFC)
    session.frequency = 50_000_000

    assert driver.start_capture(session) is CaptureError.NONE
    # 5 cycles at 100 MHz are 50 ns, 2.5 sample periods at 50 MHz
    assert driver.started[0][1].pre_trigger_samples == 100 + round(2.5 + 0.3)


def test_the_trigger_offset_needs_post_trigger_samples(make_set):
    driver = make_set(NEW_FIRMWARE, NEW_FIRMWARE)
    session = capture([0, *range(24, 40)], trigger_type=TriggerType.COMPLEX, trigger_channel=24,
                      trigger_bit_count=16, trigger_pattern=0xFFFC)
    session.post_trigger_samples = 5

    assert driver.start_capture(session) is CaptureError.BAD_PARAMS
    assert driver.started == []
