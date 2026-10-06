"""Screen readers: names of icon-only buttons and of the painted views."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from openscilab.driver.models import AnalyzerChannel, CaptureSession
from openscilab.driver.simulated import open_simulated
from openscilab.ui.accessibility import unnamed_buttons


def test_every_icon_only_button_has_a_name(shell, make_dataview):
    shell.show_start_page()
    flow = shell.new_flow()
    flow.add_node("control.timer", (0, 0))
    instrument = open_simulated("uno")
    shell.hub.add(instrument)
    card = shell.open_device_card(instrument)
    view = make_dataview()
    panel = shell.new_panel()
    shell.show_hardware()
    QApplication.processEvents()
    for document in (flow, card, view, panel):
        shell.area.activate(document)
        QApplication.processEvents()
        assert unnamed_buttons(document) == [], document.title
    assert unnamed_buttons(shell) == []


def test_painted_views_describe_themselves(shell, make_dataview):
    flow = shell.new_flow()
    flow.add_node("control.timer", (0, 0))
    QApplication.processEvents()
    assert flow.view.accessibleName() == "Flow graph" and "1 nodes" in flow.view.accessibleDescription()
    view = make_dataview()
    session = CaptureSession(frequency=1000, pre_trigger_samples=0, post_trigger_samples=100)
    session.capture_channels = [AnalyzerChannel(channel_number=0, samples=np.zeros(100, np.uint8))]
    view.load_session(session)
    assert view.sample_viewer.accessibleName() == "Waveform"
    assert "1 channels" in view.sample_viewer.accessibleDescription()


def test_status_dots_are_sharp():
    from openscilab.core.instrument import InstrumentStatus
    from openscilab.ui.documents.device import status_icon

    QApplication.instance() or QApplication([])
    pixmap = status_icon(InstrumentStatus.CONNECTED).pixmap(20, 20)
    assert pixmap.devicePixelRatio() >= 1
    assert status_icon(InstrumentStatus.CONNECTED).availableSizes()[0].width() >= 40


def test_the_capture_settings_have_pages_and_show_where_a_problem_is(shell):
    from openscilab.ui.dialogs.capture_dialog import CaptureDialog

    instrument = open_simulated("free")
    dialog = CaptureDialog(instrument.capture.driver, shell)
    assert [dialog.page_list.item(row).text() for row in range(dialog.page_list.count())] == [
        "Sampling", "Channels", "Trigger"]
    for selector in dialog.channel_selectors:
        selector.enabled = False
    dialog.page_list.setCurrentRow(2)
    assert dialog.build_session() is None  # no channel
    assert dialog.page_list.currentRow() == 1 and "channel" in dialog.validation_label.text().lower()
    dialog._update_overview()
    assert dialog.overview_label.text().startswith("0 channels")
    dialog.reject()
