"""A connected device in a few lines (core/device_summary.py) and in the device list of the sidebar
(ui/shell/device_rows.py): state, connection, the settings of the next capture and how much of the
device they use."""

from __future__ import annotations

import types

import pytest

from openscilab.core.device_summary import LimitCache, settings_line, summarize
from openscilab.core.instrument import InstrumentStatus
from openscilab.driver.base import ACQUISITION_STREAM
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated


def session(samples: int, channels=range(8), rate: int = 1_000_000, stream: bool = False) -> CaptureSession:
    capture = CaptureSession(frequency=rate, pre_trigger_samples=0, post_trigger_samples=samples,
                             trigger_type=TriggerType.IMMEDIATE,
                             acquisition_mode=ACQUISITION_STREAM if stream else "buffer")
    capture.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    return capture


@pytest.fixture
def pico():
    instrument = open_simulated("pico")
    yield instrument
    instrument.close()


def test_a_capture_into_the_device_uses_part_of_its_memory(pico):
    summary = summarize(pico, session(32_768), ("Ready", "idle"))
    assert summary.state == "Ready" and summary.level == "idle"
    assert summary.connection == "sim:pico · simulated"
    assert summary.settings == "1 MHz · 32,768 samples · 8 ch · trigger: none"
    # 8 channels: the simulated Pico holds 131,072 samples of them
    assert summary.load == pytest.approx(0.25) and summary.load_kind == "memory"
    assert summary.load_line == "32.8 ms of signal · memory 25 %"
    # a capture that fills the memory (as by default): how long it lasts, no bar
    full = summarize(pico, session(131_072 - 2, rate=20_000_000), ("Ready", "idle"))
    assert full.load_line == "6.55 ms of signal" and full.load is None and full.load_kind == ""


def test_a_stream_uses_part_of_the_link(pico):
    summary = summarize(pico, session(10_000, rate=400_000, stream=True), ("Ready", "idle"))
    # 8 channels stream at 800 kHz at most
    assert summary.load == pytest.approx(0.5) and summary.load_text == "25 ms of signal · link 50 %"
    assert summary.load_kind == "link"
    assert summary.settings.endswith("· stream")


def test_a_capture_that_arrives_shows_how_much_did(pico):
    summary = summarize(pico, session(32_768), ("Receiving 45 %", "busy"), received=0.45)
    assert summary.state == "Receiving 45 %" and summary.level == "busy"
    assert summary.load == pytest.approx(0.45) and summary.load_text == "received 45 %"
    armed = summarize(pico, session(32_768), ("Armed, waiting for the trigger", "armed"))
    assert armed.state == "Armed" and armed.level == "armed"
    failed = summarize(pico, session(32_768), ("Capture failed: no samples", "error"))
    assert failed.state == "Failed" and failed.level == "error"


def test_a_disconnected_device_and_one_without_captures(pico):
    pico.status = InstrumentStatus.DISCONNECTED
    gone = summarize(pico, session(32_768))
    assert (gone.state, gone.level, gone.settings, gone.load) == ("Disconnected", "off", "", None)
    pico.status = InstrumentStatus.SIMULATED
    offers = summarize(pico)  # (no settings of a capture: what it offers)
    assert offers.settings == "Capture · GPIO · Monitor · Analog inputs · Generator" and offers.load is None


def test_the_load_of_a_device_process_and_limits_asked_once(pico):
    pico.process = types.SimpleNamespace(cpu_load=0.123)
    summary = summarize(pico, session(32_768))
    assert summary.process_load == pytest.approx(0.123)
    assert summary.load_line == "32.8 ms of signal · memory 25 % · process 12 % CPU"
    # the bar: the processor before the memory; of the link and the processor the one closer to its limit
    assert (summary.load_kind, summary.load) == ("process", pytest.approx(0.123))
    pico.process.cpu_load = 0.95
    stream = summarize(pico, session(10_000, rate=400_000, stream=True))
    assert (stream.load_kind, stream.load) == ("process", pytest.approx(0.95))
    pico.process.cpu_load = 0.1
    assert summarize(pico, session(10_000, rate=400_000, stream=True)).load_kind == "link"
    del pico.process
    asked = []
    driver = pico.capture.driver
    real = driver.get_limits
    driver.get_limits = lambda channels, *args, **kwargs: (asked.append(tuple(channels)), real(channels))[1]
    limits = LimitCache()
    for _ in range(3):
        summarize(pico, session(32_768), limits=limits)
    assert asked == [tuple(range(8))]  # (a device in a process answers through a pipe: once)
    driver.get_limits = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gone"))
    assert summarize(pico, session(1000, channels=[0]), limits=LimitCache()).load is None


def test_the_settings_line_is_that_of_the_device_card(pico):
    assert settings_line(session(1000, channels=[0, 1], rate=250_000)) == "250 kHz · 1,000 samples · 2 ch · trigger: none"


def test_the_device_list_shows_a_row_per_connected_device(shell, monkeypatch):
    from openscilab.ui.shell.device_rows import DeviceRow

    instrument = open_simulated("pico")
    shell.hub.add(instrument)
    section = shell.devices_section
    section.refresh_open()
    assert len(section.rows) == 1 and isinstance(section.rows[0], DeviceRow)
    row = section.rows[0]
    assert section.open_list.itemWidget(section.open_list.item(0)) is row
    assert row.name_label.full_text() == "Simulation: Pico" and row.state_label.text() == "Ready"
    assert row.connection_label.full_text() == "sim:pico · simulated"
    assert row.settings_label.full_text().endswith("trigger: none") and " of signal" in row.load_label.text()
    assert row.bar.isHidden() == (row.summary.load is None)
    # a capture that runs: the row says so at the next refresh
    from openscilab.ui.devices.capture import CaptureController, capture_controller

    controller = capture_controller(instrument)
    monkeypatch.setattr(CaptureController, "is_capturing", property(lambda self: True))
    controller._running, controller.received = True, 0.4
    section.update_rows()
    assert row.state_label.text() == "Receiving 40 %" and row.load_label.text() == "received 40 %"
    controller._running, controller.received = False, None
    monkeypatch.undo()
    # the list under the row takes the clicks: the device card opens
    opened = []
    section.instrument_activated.connect(opened.append)
    section.open_list.itemClicked.emit(section.open_list.item(0))
    assert opened == [instrument]
    shell.disconnect_instrument(instrument, ask=False)
    assert not section.rows


def test_the_rows_refresh_while_the_list_is_shown(shell):
    section = shell.devices_section
    shell.activity_bar.select("devices")
    assert section.isVisible() and section.status_timer.isActive()
    shell.activity_bar.select("project")
    assert not section.status_timer.isActive()
