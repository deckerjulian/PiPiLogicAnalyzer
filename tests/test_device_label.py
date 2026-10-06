"""The source of a data view: chosen in its capture tool bar, its device card one action away."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_the_source_of_the_data_opens_its_device_card(application, make_dataview):
    from openscilab.driver.simulated import open_simulated

    window = make_dataview()
    requested = []
    window.device_card_requested.connect(requested.append)
    try:
        assert window.device_combo.currentText() == "No device"
        assert not window.action_device_card.isEnabled()  # no device

        instrument = open_simulated("free", name="Bench <1>")
        window.hub.add(instrument)
        window.use_instrument(instrument)
        assert window.device_combo.currentText() == "Bench <1>"  # (plain text: nothing to escape)
        window.action_device_card.trigger()
        assert requested == [instrument]
        assert not window.repeat_button.isHidden() and window.action_device_card.isEnabled()
    finally:
        window.close()
