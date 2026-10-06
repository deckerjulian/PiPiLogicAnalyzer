# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A device for openSciLab on any computer with Python: a script describes its inputs, outputs,
commands and synchronisation signals and connects to openSciLab, which uses it like any other
instrument. Only the standard library is needed (numpy is used when it is there).

Copy this folder to the other computer (or install it, see ``docs/remote.md``)::

    from openscilab_device import Device

    dev = Device("climate-pi", server="192.168.1.20", token="...")
    temperature = dev.input("temperature", kind="scalar", unit="°C")
    dev.start()
    while True:
        temperature.send(read_sensor())
        time.sleep(1)
"""

from .device import Command, Device, Input, Output, SyncInput, SyncOutput
from .protocol import DEFAULT_PORT, PROTOCOL

__version__ = "1.0.0"
__all__ = ["DEFAULT_PORT", "PROTOCOL", "Command", "Device", "Input", "Output", "SyncInput", "SyncOutput"]
