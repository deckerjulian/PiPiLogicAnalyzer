# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Built-in plugin: DreamSourceLab DSLogic analyzers over USB (``driver/dslogic``).

``dslogic:1:4``
    The DSLogic at USB bus 1, address 4 (``dslogic`` alone: the first one).
"""

from __future__ import annotations

from typing import Optional

from ..driver import kinds
from ..driver.base import AnalyzerDriverBase, DeviceConnectionError


def open_dslogic(rest: str, download_bitstream: bool = False) -> AnalyzerDriverBase:
    """Raises :class:`~openscilab.driver.dslogic.driver.BitstreamMissingError` (a
    ``DeviceConnectionError``) when the FPGA bitstream is missing, unless ``download_bitstream``
    allows fetching it from the DSView repository."""
    from ..driver.dslogic import driver as dslogic_driver
    from ..driver.dslogic import resources as dslogic_resources
    from ..driver.dslogic import usb as dslogic_usb

    info: Optional[dslogic_usb.UsbDeviceInfo]
    if rest:
        info = dslogic_usb.find_device(rest)
        if info is None:
            raise DeviceConnectionError(f"No DSLogic at USB {rest}.")
    else:
        found = dslogic_usb.list_devices()
        if not found:
            raise DeviceConnectionError("No DSLogic is connected.")
        info = found[0]

    info = dslogic_driver.prepare(info)
    try:
        return dslogic_driver.DSLogicDriver(info)
    except dslogic_driver.BitstreamMissingError as missing:
        if not download_bitstream or not dslogic_resources.downloadable(missing.name):
            raise
        try:
            dslogic_resources.download(missing.name)
        except dslogic_resources.ResourceError as error:
            raise DeviceConnectionError(f"The bitstream could not be downloaded: {error}") from error
        return dslogic_driver.DSLogicDriver(info)


def detect() -> list[tuple[str, str]]:
    from ..driver.dslogic import usb as dslogic_usb

    return [(info.location, info.description) for info in dslogic_usb.list_devices()]


kinds.register("dslogic", open_dslogic, title="DSLogic", detect=detect, process=True)


def setup_ui() -> None:
    from ..ui.devices import register_backend
    from ..ui.devices.dslogic import DSLogicBackend

    register_backend(DSLogicBackend())
