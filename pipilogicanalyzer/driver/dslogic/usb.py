# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""USB access for the DSLogic driver.

:class:`UsbDevice` is the small interface the driver needs; :class:`PyUsbDevice` implements it
with pyusb and the libusb library bundled by the ``libusb-package`` package, so the packaged
application does not depend on a system libusb. The tests replace it with a fake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import protocol

log = logging.getLogger("pipilogicanalyzer.driver.dslogic")


class UsbError(OSError):
    """A USB operation failed (device gone, access denied, ...)."""


class UsbTimeout(UsbError):
    """A bulk read returned no data in time."""


@dataclass
class UsbDeviceInfo:
    """A DSLogic found on the bus."""

    bus: int
    address: int
    pid: int
    model: str
    serial_number: str = ""
    super_speed: bool = False
    #: the board runs the firmware this driver speaks ("USB-based DSL Instrument v2")
    firmware_ready: bool = True

    @property
    def location(self) -> str:
        return f"{self.bus}:{self.address}"

    @property
    def description(self) -> str:
        speed = "USB 3" if self.super_speed else "USB 2"
        return f"{self.model} ({speed}, {self.location})"


class UsbDevice:
    """Operations the driver uses; everything raises :class:`UsbError` on failure."""

    info: UsbDeviceInfo

    def control_out(self, request: int, data: bytes, timeout_ms: int = 3000) -> None:
        raise NotImplementedError

    def control_in(self, request: int, length: int, timeout_ms: int = 3000) -> bytes:
        raise NotImplementedError

    def bulk_write(self, endpoint: int, data: bytes, timeout_ms: int = 1000) -> int:
        raise NotImplementedError

    def bulk_read(self, endpoint: int, length: int, timeout_ms: int) -> bytes:
        """Raises :class:`UsbTimeout` when nothing arrived within ``timeout_ms``."""
        raise NotImplementedError

    def claim(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# ------------------------------------------------------------------- pyusb
def _backend():
    """The libusb backend, or ``None`` when neither the bundled nor a system libusb is found."""
    try:
        import libusb_package

        backend = libusb_package.get_libusb1_backend()
        if backend is not None:
            return backend
    except Exception as error:  # noqa: BLE001 - optional dependency
        log.debug("libusb-package unavailable: %s", error)
    try:
        import usb.backend.libusb1

        return usb.backend.libusb1.get_backend()
    except Exception as error:  # noqa: BLE001 - optional dependency
        log.debug("pyusb unavailable: %s", error)
    return None


def backend_available() -> bool:
    return _backend() is not None


def _string(device, index: int) -> str:
    import usb.util

    try:
        return usb.util.get_string(device, index) or ""
    except Exception:  # noqa: BLE001 - no permission or no string
        return ""


def _is_super_speed(device) -> bool:
    import usb.util

    speed = getattr(device, "speed", None)
    return speed is not None and speed >= getattr(usb.util, "SPEED_SUPER", 4)


def list_devices() -> list[UsbDeviceInfo]:
    """Every DSLogic model this driver supports; empty without libusb."""
    backend = _backend()
    if backend is None:
        return []
    try:
        import usb.core

        devices = list(usb.core.find(find_all=True, idVendor=protocol.VENDOR_ID, backend=backend))
    except Exception as error:  # noqa: BLE001 - the bus could not be enumerated
        log.debug("USB enumeration failed: %s", error)
        return []

    found: list[UsbDeviceInfo] = []
    for device in devices:
        profile = protocol.PROFILES.get(device.idProduct)
        if profile is None:
            continue
        product = _string(device, device.iProduct)
        found.append(
            UsbDeviceInfo(
                bus=device.bus,
                address=device.address,
                pid=device.idProduct,
                model=profile.model,
                serial_number=_string(device, device.iSerialNumber),
                super_speed=_is_super_speed(device),
                # Without permission the strings cannot be read; opening tells more then.
                firmware_ready=not product or product.startswith(protocol.PRODUCT_V2),
            )
        )
    return found


class PyUsbDevice(UsbDevice):
    """A DSLogic opened through pyusb."""

    def __init__(self, info: UsbDeviceInfo) -> None:
        backend = _backend()
        if backend is None:
            raise UsbError(
                "No USB library (libusb) found. Install the Python package 'libusb-package' "
                "or the libusb library of your system."
            )
        import usb.core

        self.info = info
        device = usb.core.find(
            idVendor=protocol.VENDOR_ID,
            backend=backend,
            custom_match=lambda d: d.bus == info.bus and d.address == info.address,
        )
        if device is None:
            raise UsbError(f"{info.description} is no longer connected.")
        self._device = device
        self._claimed = False

    @staticmethod
    def _wrap(error: Exception) -> UsbError:
        import usb.core

        if isinstance(error, usb.core.USBTimeoutError):
            return UsbTimeout(str(error))
        text = str(error)
        if getattr(error, "errno", None) == 13 or "access denied" in text.lower():
            return UsbError(
                "Access to the DSLogic was denied. On Linux install the udev rule "
                "(packaging/linux/60-dslogic.rules); on Windows the WinUSB driver of DSView."
            )
        return UsbError(text)

    def control_out(self, request: int, data: bytes, timeout_ms: int = 3000) -> None:
        try:
            self._device.ctrl_transfer(protocol.REQUEST_TYPE_OUT, request, 0, 0, data, timeout_ms)
        except Exception as error:  # noqa: BLE001 - normalised
            raise self._wrap(error) from error

    def control_in(self, request: int, length: int, timeout_ms: int = 3000) -> bytes:
        try:
            return bytes(self._device.ctrl_transfer(protocol.REQUEST_TYPE_IN, request, 0, 0, length, timeout_ms))
        except Exception as error:  # noqa: BLE001 - normalised
            raise self._wrap(error) from error

    def bulk_write(self, endpoint: int, data: bytes, timeout_ms: int = 1000) -> int:
        try:
            return int(self._device.write(endpoint, data, timeout_ms))
        except Exception as error:  # noqa: BLE001 - normalised
            raise self._wrap(error) from error

    def bulk_read(self, endpoint: int, length: int, timeout_ms: int) -> bytes:
        try:
            return bytes(self._device.read(endpoint, length, timeout_ms))
        except Exception as error:  # noqa: BLE001 - normalised
            raise self._wrap(error) from error

    def claim(self) -> None:
        import usb.util

        try:
            try:
                if self._device.is_kernel_driver_active(protocol.USB_INTERFACE):
                    self._device.detach_kernel_driver(protocol.USB_INTERFACE)
            except (NotImplementedError, Exception):  # noqa: BLE001 - not supported everywhere
                pass
            try:
                self._device.set_configuration(protocol.USB_CONFIGURATION)
            except Exception:  # noqa: BLE001 - already configured or busy; claiming decides
                pass
            usb.util.claim_interface(self._device, protocol.USB_INTERFACE)
            self._claimed = True
        except Exception as error:  # noqa: BLE001 - normalised
            raise UsbError(
                f"The DSLogic could not be opened ({error}). Is it used by another program, "
                "e.g. DSView?"
            ) from error

    def close(self) -> None:
        import usb.util

        try:
            if self._claimed:
                usb.util.release_interface(self._device, protocol.USB_INTERFACE)
            usb.util.dispose_resources(self._device)
        except Exception:  # noqa: BLE001 - best effort
            pass
        self._claimed = False


def open_device(info: UsbDeviceInfo) -> UsbDevice:
    return PyUsbDevice(info)


def upload_fx2_firmware(info: UsbDeviceInfo, image: bytes) -> None:
    """Cypress EZ-USB RAM download (``ezusb_upload_firmware`` of DSView): reset, write, run."""
    device = PyUsbDevice(info)
    try:
        try:
            device._device.set_configuration(protocol.USB_CONFIGURATION)
        except Exception:  # noqa: BLE001 - may already be configured
            pass

        def ezusb_write(address: int, data: bytes) -> None:
            try:
                device._device.ctrl_transfer(protocol.REQUEST_TYPE_OUT, 0xA0, address, 0, data, 3000)
            except Exception as error:  # noqa: BLE001 - normalised
                raise PyUsbDevice._wrap(error) from error

        ezusb_write(0xE600, b"\x01")  # CPU reset on
        for offset in range(0, len(image), 4096):
            ezusb_write(offset, image[offset : offset + 4096])
        ezusb_write(0xE600, b"\x00")  # run
    finally:
        device.close()


def find_device(location: str) -> Optional[UsbDeviceInfo]:
    for info in list_devices():
        if info.location == location:
            return info
    return None
