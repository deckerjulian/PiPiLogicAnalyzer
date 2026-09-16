"""Board self-test and simulated capture: driver protocol and GUI."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from pipilogicanalyzer.core import simulation
from pipilogicanalyzer.core.simulation import SimulationPattern
from pipilogicanalyzer.driver import protocol
from pipilogicanalyzer.driver.analyzer import PiPiLogicAnalyzerDriver
from pipilogicanalyzer.driver.base import (
    CaptureError,
    CaptureMode,
    SelfTestResult,
    UnsupportedFeatureError,
    parse_self_test_line,
)
from pipilogicanalyzer.driver.emulated import EmulatedAnalyzerDriver
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from pipilogicanalyzer.ui.dialogs.board_test_dialog import BoardTestDialog
from pipilogicanalyzer.ui.dialogs.simulation_dialog import SimulationDialog

from test_driver import FakeTransport


@pytest.fixture
def device(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(
        "pipilogicanalyzer.driver.analyzer.SerialTransport", lambda *args, **kwargs: transport
    )
    driver = PiPiLogicAnalyzerDriver("/dev/fake")
    driver.test_transport = transport  # type: ignore[attr-defined]
    return driver


def simulation_session(channels: int = 6, samples: int = 1000, pattern=SimulationPattern.PROTOCOLS):
    session = CaptureSession(
        frequency=1_000_000,
        pre_trigger_samples=0,
        post_trigger_samples=samples,
        trigger_type=TriggerType.SIMULATION,
        trigger_pattern=int(pattern),
    )
    session.capture_channels = [AnalyzerChannel(channel_number=index) for index in range(channels)]
    return session


# ------------------------------------------------------------------- parsing
def test_self_test_lines_are_parsed():
    result = parse_self_test_line("SELFTEST:TRIGGER_LINK:FAIL:GPIO 0 and GPIO 1: not connected\n")
    assert result == SelfTestResult("TRIGGER_LINK", "FAIL", "GPIO 0 and GPIO 1: not connected")
    assert result.title == "Trigger link" and result.severity == "fail"
    assert parse_self_test_line("SELFTEST:CH3:STUCK_HIGH:GPIO 4").title == "Channel 3"
    assert parse_self_test_line("SELFTEST:CH3:STUCK_HIGH:GPIO 4").severity == "warning"
    assert parse_self_test_line("SELFTEST:BOARD:INFO").detail == ""
    assert parse_self_test_line("CAPTURE_STARTED") is None


# -------------------------------------------------------------------- driver
def test_capabilities_are_queried_once(device):
    device.test_transport.queue_response("CAPS:SELFTEST,SIMULATION")
    assert device.capabilities() == {"SELFTEST", "SIMULATION"}
    written = bytes(device.test_transport.written)
    assert written.endswith(protocol.command_packet(protocol.CMD_CAPABILITIES))

    assert device.capabilities() == {"SELFTEST", "SIMULATION"}
    assert bytes(device.test_transport.written) == written


def test_original_firmware_has_no_capabilities(device):
    device.test_transport.queue_response("ERR_UNKNOWN_MSG")
    assert device.capabilities() == frozenset()
    with pytest.raises(UnsupportedFeatureError):
        device.run_self_test()


def test_self_test_results_are_collected(device):
    transport = device.test_transport
    for line in (
        "CAPS:SELFTEST,SIMULATION",
        "SELFTEST:BOARD:INFO:PICO V6_5",
        "SELFTEST:CH1:OK:GPIO 2",
        "SELFTEST:CH2:STUCK_LOW:GPIO 3",
        "SELFTEST_END",
    ):
        transport.queue_response(line)

    results = device.run_self_test()

    assert [(result.item, result.status) for result in results] == [
        ("BOARD", "INFO"), ("CH1", "OK"), ("CH2", "STUCK_LOW"),
    ]
    assert bytes(transport.written).endswith(protocol.command_packet(protocol.CMD_SELF_TEST))


def test_simulated_capture_request(device, monkeypatch):
    transport = device.test_transport
    transport.queue_response("CAPS:SELFTEST,SIMULATION")
    transport.queue_response("CAPTURE_STARTED")
    monkeypatch.setattr(device, "_read_capture", lambda *args: None)
    session = simulation_session()

    assert device.start_capture(session) is CaptureError.NONE

    request = device.compose_request(session, CaptureMode.CHANNELS_8)
    raw = request.pack(device.request_layout)
    assert raw[0] == int(TriggerType.SIMULATION)
    assert raw[4] == int(SimulationPattern.PROTOCOLS)
    assert bytes(transport.written).endswith(protocol.command_packet(protocol.CMD_START_CAPTURE, raw))


def test_simulation_needs_firmware_support(device):
    device.test_transport.queue_response("ERR_UNKNOWN_MSG")
    assert device.start_capture(simulation_session()) is CaptureError.UNSUPPORTED


def test_simulation_rejects_unknown_patterns(device):
    session = simulation_session()
    session.trigger_pattern = 7
    assert not device.validate_settings(session, session.total_samples)


def test_emulated_driver_computes_the_simulation():
    driver = EmulatedAnalyzerDriver(1)
    session = simulation_session(channels=8, samples=2000)
    done = threading.Event()
    results = []

    assert driver.start_capture(session, lambda args: (results.append(args), done.set())) is CaptureError.NONE
    assert done.wait(5) and results[0].success

    expected = simulation.generate(SimulationPattern.PROTOCOLS, 8, 2000)
    for channel, samples in zip(session.capture_channels, expected):
        assert np.array_equal(channel.samples, samples)
    assert driver.start_capture(CaptureSession()) is CaptureError.HARDWARE_ERROR


# ----------------------------------------------------------------------- GUI
@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


class FakeTestDriver(EmulatedAnalyzerDriver):
    def __init__(self, outcome):
        super().__init__(1)
        self.outcome = outcome

    def run_self_test(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_board_test_dialog_lists_the_results(application):
    results = [
        SelfTestResult("RAM", "OK", "131072 bytes"),
        SelfTestResult("CH2", "STUCK_HIGH", "GPIO 3"),
        SelfTestResult("TRIGGER_LINK", "FAIL", "not connected"),
    ]
    dialog = BoardTestDialog(FakeTestDriver(results))
    dialog._on_done(results)

    assert dialog.table.rowCount() == 3
    assert dialog.table.item(1, 0).text() == "Channel 2"
    assert dialog.table.item(1, 1).text() == "Warning"
    assert "signal connected" in dialog.table.item(1, 2).text()
    assert dialog.summary.text() == "1 test(s) failed, 1 warning(s), 1 passed."


def test_board_test_dialog_runs_the_worker(application):
    dialog = BoardTestDialog(FakeTestDriver([SelfTestResult("RAM", "OK")]))
    dialog.start()
    deadline = time.monotonic() + 5
    while dialog.table.rowCount() == 0 and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert dialog.summary.text() == "All 1 tests passed."


def test_board_test_dialog_explains_missing_firmware_support(application):
    dialog = BoardTestDialog(FakeTestDriver(UnsupportedFeatureError("no")))
    dialog._on_done(UnsupportedFeatureError("no"))
    assert "firmware/" in dialog.summary.text()


def test_simulation_dialog_builds_the_session(application):
    dialog = SimulationDialog(EmulatedAnalyzerDriver(1), on_board=False, board_connected=False)
    dialog.channels_box.setValue(7)
    session = dialog.build_session()

    assert session.trigger_type is TriggerType.SIMULATION
    assert session.trigger_pattern == int(SimulationPattern.PROTOCOLS)
    assert [channel.channel_name for channel in session.capture_channels][:2] == ["UART TX", "SPI CLK"]
    assert len(session.capture_channels) == 7
    assert "UART TX" in dialog.description.text()
    assert dialog.add_decoders

    dialog.pattern_box.setCurrentIndex(dialog.pattern_box.findData(int(SimulationPattern.COUNTER)))
    assert not dialog.add_decoders and not dialog.decoders_box.isEnabled()


def test_main_window_simulates_without_a_board(application, monkeypatch):
    from pipilogicanalyzer.ui import main_window as main_window_module
    from pipilogicanalyzer.ui.main_window import MainWindow

    class AcceptingDialog(SimulationDialog):
        def exec(self):
            self.channels_box.setValue(8)
            self.samples_box.setValue(20_000)
            self._accept()
            return True

    monkeypatch.setattr(main_window_module, "SimulationDialog", AcceptingDialog)
    monkeypatch.setattr("pipilogicanalyzer.ui.messages.error", lambda *args, **kwargs: pytest.fail(str(args)))
    window = MainWindow()
    try:
        window.simulated_capture()
        deadline = time.monotonic() + 10
        while window.model.session is None and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.01)

        session = window.model.session
        assert session is not None and session.trigger_type is TriggerType.SIMULATION
        assert window.model.sample_count == 20_000
        if window.provider.registry.get("uart") is not None:
            assert {"uart", "spi", "i2c"} <= {i.decoder_id for i in window.provider.instances}
    finally:
        window.close()
