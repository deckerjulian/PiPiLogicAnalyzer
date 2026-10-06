# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""DreamSourceLab DSLogic analyzers over USB."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QFileDialog, QWidget

from ...driver.base import DeviceConnectionError
from ...driver.dslogic import driver as dslogic_driver
from ...driver.dslogic import resources as dslogic_resources
from ...driver.dslogic import usb as dslogic_usb
from .. import background, messages
from . import DeviceBackend, DeviceEntry

BACKEND_ID = "dslogic"


def usb_entry(location: str, label: str = "") -> DeviceEntry:
    return DeviceEntry(BACKEND_ID, "usb", location, label)


class DSLogicBackend(DeviceBackend):
    id = BACKEND_ID

    def detected(self) -> list[DeviceEntry]:
        return [usb_entry(device.location, device.description) for device in dslogic_usb.list_devices()]

    def address(self, entry: DeviceEntry, parent: QWidget) -> Optional[str]:
        if dslogic_usb.find_device(entry.value) is None:
            raise DeviceConnectionError("The DSLogic is no longer connected. Press Refresh.")
        return f"dslogic:{entry.value}"

    def open_instrument(self, entry: DeviceEntry, parent: QWidget):
        # finding it, loading its firmware and configuring its FPGA take seconds: not in the
        # thread of the window (the questions about the bitstream are asked there)
        while True:
            try:
                return super().open_instrument(entry, parent)
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
            "openSciLab. They are taken from an installed DSView (its 'res' folder), or "
            "downloaded once from the DSView repository on GitHub "
            f"(commit {dslogic_resources.DSVIEW_COMMIT[:7]}) into the settings directory.",
        )
        if choice is None:
            return False
        if options[choice].startswith("Download"):
            try:
                background.run(parent, f"Downloading {missing.name}...",
                               lambda: dslogic_resources.download(missing.name))
            except background.Cancelled:
                return False
            except dslogic_resources.ResourceError as error:
                messages.error(parent, "Download", "The bitstream could not be downloaded.", str(error))
                return False
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
