# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""DreamSourceLab DSLogic analyzers over USB."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from ...driver.base import AnalyzerDriverBase, DeviceConnectionError
from ...driver.dslogic import driver as dslogic_driver
from ...driver.dslogic import resources as dslogic_resources
from ...driver.dslogic import usb as dslogic_usb
from .. import messages
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "dslogic"


def usb_entry(location: str, label: str = "") -> DeviceEntry:
    return DeviceEntry(BACKEND_ID, "usb", location, label)


class DSLogicBackend(DeviceBackend):
    id = BACKEND_ID

    def detected(self) -> list[DeviceEntry]:
        return [usb_entry(device.location, device.description) for device in dslogic_usb.list_devices()]

    def connect(self, entry: DeviceEntry, parent: QWidget) -> Optional[AnalyzerDriverBase]:
        info = dslogic_usb.find_device(entry.value)
        if info is None:
            raise DeviceConnectionError("The DSLogic is no longer connected. Press Refresh.")
        info = dslogic_driver.prepare(info)
        while True:
            try:
                return dslogic_driver.DSLogicDriver(info)
            except dslogic_driver.BitstreamMissingError as missing:
                if not self._provide_bitstream(missing, parent):
                    return None

    def _provide_bitstream(self, missing: "dslogic_driver.BitstreamMissingError", parent: QWidget) -> bool:
        """Ask for the FPGA bitstream of a DSLogic; ``True`` when it may be there now."""
        options = ["Choose DSView folder..."]
        if dslogic_resources.downloadable(missing.name):
            options.insert(0, "Download from DSView")
        choice = messages.choose(
            parent,
            "FPGA bitstream needed",
            f"The {missing.model} needs its FPGA bitstream {missing.name}.",
            options,
            "The bitstreams belong to DreamSourceLab's DSView and are not part of "
            "PiPiLogicAnalyzer. They are taken from an installed DSView (its 'res' folder), or "
            "downloaded once from the DSView repository on GitHub "
            f"(commit {dslogic_resources.DSVIEW_COMMIT[:7]}) into the settings directory.",
        )
        if choice is None:
            return False
        if options[choice].startswith("Download"):
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                dslogic_resources.download(missing.name)
            except dslogic_resources.ResourceError as error:
                messages.error(parent, "Download", "The bitstream could not be downloaded.", str(error))
                return False
            finally:
                QApplication.restoreOverrideCursor()
            return True
        folder = QFileDialog.getExistingDirectory(parent, "DSView 'res' folder")
        if not folder:
            return False
        if dslogic_resources.find(missing.name, [folder]) is None:
            messages.warning(
                parent, "FPGA bitstream", f"{missing.name} is not in this folder.",
                "Choose the 'res' folder of DSView, e.g. C:\\Program Files\\DSView\\res.",
            )
            return False
        dslogic_resources.set_chosen_directory(folder)
        return True
