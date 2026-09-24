# Copyright (C) 2026 Julian Decker
# Copyright (C) DreamSourceLab, DSView (libsigrok4DSL/hardware/DSL)
#
# Part of PiPiLogicAnalyzer. The sequences follow the DSLogic driver of DSView (GPL-3.0-or-later),
# see protocol.py.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Driver for the DreamSourceLab DSLogic Plus, U3Pro16 and U3Pro32."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Sequence

import numpy as np

from ..base import (
    ACQUISITION_BUFFER,
    ACQUISITION_STREAM,
    CAPABILITY_IMMEDIATE_TRIGGER,
    CAPABILITY_PATTERN_GROUPS,
    CAPABILITY_THRESHOLD,
    AnalyzerDeviceInfo,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    DeviceConnectionError,
)
from ..models import CaptureSession, TriggerType
from . import protocol, resources
from . import usb as usb_access
from .protocol import AcquisitionMode, CaptureSetup, TriggerSpec
from .usb import UsbDevice, UsbDeviceInfo, UsbError, UsbTimeout

log = logging.getLogger("pipilogicanalyzer.driver.dslogic")

#: Default input threshold of DSView
DEFAULT_THRESHOLD = 1.0
#: The application keeps one byte per sample and channel: limit a capture to about 1 GiB of them
MAX_SAMPLE_BYTES = 1 << 30
#: Time to wait for the status bits of the FPGA
STATUS_TIMEOUT = 2.0
#: A stream capture fails when no data arrived for this long
STREAM_STALL_TIMEOUT = 3.0
#: Bulk reads are split into short timeouts so that an abort is noticed quickly
READ_SLICE_MS = 200


class BitstreamMissingError(DeviceConnectionError):
    """The FPGA bitstream of the board is not available; it can be downloaded or chosen."""

    def __init__(self, name: str, model: str) -> None:
        super().__init__(
            f"The FPGA bitstream {name} of the {model} was not found. It comes with DSView: "
            "choose the 'res' folder of a DSView installation or download the file."
        )
        self.name = name
        self.model = model


class FirmwareMissingError(DeviceConnectionError):
    """The board does not run the DSView v2 firmware and none could be loaded."""


def _acquisition(mode: Optional[str]) -> AcquisitionMode:
    return AcquisitionMode.STREAM if mode == ACQUISITION_STREAM else AcquisitionMode.BUFFER


def prepare(
    info: UsbDeviceInfo,
    list_devices: Callable[[], list[UsbDeviceInfo]] = usb_access.list_devices,
    upload: Callable[[UsbDeviceInfo, bytes], None] = usb_access.upload_fx2_firmware,
    wait: float = 5.0,
) -> UsbDeviceInfo:
    """Makes sure the board runs the firmware of this driver; returns its (new) bus position.

    The Plus, U3Pro16 and U3Pro32 normally start their firmware from their own memory. Only when
    one does not (an FX2 board still waiting for a RAM download) is DSView's firmware image
    loaded, if it can be found.
    """
    if info.firmware_ready:
        return info
    profile = protocol.PROFILES[info.pid]
    if profile.usb3:
        raise FirmwareMissingError(
            f"The {info.model} does not run the DSView firmware. Connect it once with DSView to "
            "update its firmware, then try again."
        )
    try:
        image = resources.load(profile.firmware)
    except resources.ResourceError as error:
        raise FirmwareMissingError(
            f"The {info.model} needs its firmware {profile.firmware}, which comes with DSView. "
            "Install DSView or choose its 'res' folder."
        ) from error

    upload(info, image)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.3)
        for candidate in list_devices():
            if candidate.pid == info.pid and candidate.firmware_ready and (
                not info.serial_number or candidate.serial_number == info.serial_number
            ):
                return candidate
    raise FirmwareMissingError(f"The {info.model} did not restart after loading its firmware.")


class DSLogicDriver(AnalyzerDriverBase):
    """One DSLogic analyzer on USB."""

    def __init__(
        self,
        info: UsbDeviceInfo,
        open_device: Callable[[UsbDeviceInfo], UsbDevice] = usb_access.open_device,
        load_resource: Callable[[str], bytes] = resources.load,
        read_delay: float = 0.01,
    ) -> None:
        super().__init__()
        if info.pid not in protocol.PROFILES:
            raise DeviceConnectionError(f"Unsupported DSLogic (USB PID 0x{info.pid:04X})")
        self.info = info
        self.profile = protocol.PROFILES[info.pid]
        self.connection_string = f"usb:{info.location}"
        self._load_resource = load_resource
        #: DSView waits 10 ms between announcing and fetching a control read
        self._read_delay = read_delay
        self._lock = threading.RLock()
        self._abort = threading.Event()
        self._capturing = False
        self._capture_thread: Optional[threading.Thread] = None
        self._threshold: Optional[float] = None
        self.firmware_version = (0, 0)
        self.hdl_version = 0

        try:
            self._device = open_device(info)
        except UsbError as error:
            raise DeviceConnectionError(str(error)) from error
        try:
            self._open()
        except Exception:
            self._device.close()
            raise

    # ---------------------------------------------------------- control I/O
    def _write(self, dest: int, data: bytes = b"", offset: int = 0) -> None:
        log.debug("CTL_WR dest=%d offset=0x%02X data=%s", dest, offset, data.hex())
        self._device.control_out(protocol.CMD_CTL_WR, protocol.ctl_write(dest, data, offset))

    def _read(self, dest: int, size: int, offset: int = 0) -> bytes:
        self._device.control_out(protocol.CMD_CTL_RD_PRE, protocol.ctl_header(dest, size, offset))
        if self._read_delay:
            time.sleep(self._read_delay)
        data = self._device.control_in(protocol.CMD_CTL_RD, size)
        log.debug("CTL_RD dest=%d offset=0x%02X -> %s", dest, offset, data.hex())
        if len(data) < size:
            raise UsbError(f"short control read ({len(data)} of {size} bytes)")
        return data

    def _drain_input(self, limit: float = 0.5) -> None:
        """Discards data left in the IN endpoint, e.g. by an aborted capture."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            try:
                data = self._device.bulk_read(protocol.EP_IN, 1 << 20, 20)
            except UsbTimeout:
                return
            if not data:
                return
            log.debug("Discarded %d stale bytes", len(data))

    def _status(self) -> int:
        return self._read(protocol.CTL_HW_STATUS, 1)[0]

    def _wait_status(self, bit: int, what: str, timeout: float = STATUS_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self._status() & bit:
                return
            if time.monotonic() > deadline:
                raise DeviceConnectionError(f"The DSLogic did not report {what}.")

    def write_register(self, address: int, value: int) -> None:
        self._write(protocol.CTL_I2C_REG, bytes([value & 0xFF]), offset=address)

    # ----------------------------------------------------------------- open
    def _open(self) -> None:
        try:
            version = self._read(protocol.CTL_FW_VERSION, 2)
            self.firmware_version = (version[0], version[1])
            if version[0] != protocol.REQUIRED_FIRMWARE_MAJOR:
                raise DeviceConnectionError(
                    f"The {self.profile.model} runs firmware {version[0]}.{version[1]}; version "
                    f"{protocol.REQUIRED_FIRMWARE_MAJOR}.x of DSView is required. Update it with DSView."
                )
            self._device.claim()

            if self._status() & protocol.FPGA_DONE:
                self.write_register(protocol.CTR0_ADDR, protocol.CTR0_NONE)
                self.hdl_version = self._read(protocol.CTL_I2C_STATUS, protocol.HDL_VERSION_ADDR + 1)[
                    protocol.HDL_VERSION_ADDR
                ]
                if self.hdl_version != protocol.HDL_VERSION:
                    # Loaded by another DSView version: load the matching bitstream again.
                    log.debug("FPGA version 0x%02X, reconfiguring", self.hdl_version)
                    self._configure_fpga()
            else:
                self._configure_fpga()

            self.write_register(protocol.CTR0_ADDR, protocol.CTR0_NONE)
            self.set_threshold(DEFAULT_THRESHOLD)
            if self.profile.usb3:
                for address, values, delay_ms in protocol.ADC_CLOCK_INIT:
                    if delay_ms:
                        time.sleep(delay_ms / 1000)
                    for value in values:
                        self.write_register(address, value)
        except UsbError as error:
            raise DeviceConnectionError(f"Communication with the {self.profile.model} failed: {error}") from error
        log.debug(
            "%s opened: firmware %d.%d, FPGA 0x%02X",
            self.profile.model, *self.firmware_version, self.hdl_version,
        )

    def _configure_fpga(self) -> None:
        """``dsl_fpga_config``: loads the bitstream through the FX2/FX3."""
        name = self.profile.bitstream
        try:
            bitstream = self._load_resource(name)
        except (resources.ResourceError, OSError) as error:
            raise BitstreamMissingError(name, self.profile.model) from error

        self._write(protocol.CTL_PROG_B, bytes([~protocol.WR_PROG_B & 0xFF]))
        self._write(protocol.CTL_LED, bytes([~(protocol.LED_GREEN | protocol.LED_RED) & 0xFF]))
        self._write(protocol.CTL_PROG_B, bytes([protocol.WR_PROG_B]))
        self._wait_status(protocol.FPGA_INIT_B, "FPGA INIT_B")
        self._write(protocol.CTL_INTRDY, bytes([~protocol.WR_INTRDY & 0xFF]))
        self._write(protocol.CTL_BULK_WR, protocol.length24(len(bitstream)))
        written = self._device.bulk_write(protocol.EP_OUT, bitstream, timeout_ms=5000)
        if written != len(bitstream):
            raise DeviceConnectionError(f"FPGA configuration sent {written} of {len(bitstream)} bytes.")
        self._write(protocol.CTL_INTRDY, bytes([protocol.WR_INTRDY]))
        self._wait_status(protocol.GPIF_DONE, "the end of the FPGA data")
        self._write(protocol.CTL_INTRDY, bytes([~protocol.WR_INTRDY & 0xFF]))
        self._wait_status(protocol.FPGA_DONE, "a configured FPGA (wrong bitstream?)")
        self._write(protocol.CTL_LED, bytes([protocol.LED_GREEN]))
        self._write(protocol.CTL_WORDWIDE, bytes([protocol.WR_WORDWIDE]))
        self.hdl_version = protocol.HDL_VERSION
        log.debug("FPGA configured with %s (%d bytes)", name, len(bitstream))

    def set_threshold(self, voltage: float) -> None:
        if self._threshold == voltage:
            return
        self.write_register(protocol.VTH_ADDR, protocol.threshold_code(voltage, self.profile.max25_vth))
        self._threshold = voltage

    # ----------------------------------------------------------- properties
    @property
    def device_version(self) -> Optional[str]:
        return self.profile.model

    @property
    def channel_count(self) -> int:
        return self.profile.channels

    @property
    def max_frequency(self) -> int:
        return max(
            protocol.max_rate(self.profile, self.info.super_speed, [0], mode) for mode in AcquisitionMode
        )

    @property
    def min_frequency(self) -> int:
        return min(mode.min_rate for mode in self.profile.modes(self.info.super_speed))

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def max_loop_count(self) -> int:
        return 0

    @property
    def buffer_size(self) -> int:
        return self.profile.hw_depth // 8

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.DSLOGIC

    @property
    def is_network(self) -> bool:
        return False

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def capabilities(self) -> frozenset[str]:
        return frozenset({
            CAPABILITY_IMMEDIATE_TRIGGER,
            CAPABILITY_THRESHOLD,
            f"{CAPABILITY_PATTERN_GROUPS}0-{self.channel_count - 1}",
        })

    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        return ((0, self.channel_count),)

    def has_external_trigger(self) -> bool:
        return False

    def acquisition_modes(self) -> tuple[str, ...]:
        return (ACQUISITION_BUFFER, ACQUISITION_STREAM)

    def sample_rates(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> list[int]:
        return protocol.available_rates(
            self.profile, self.info.super_speed, list(channels) or [0], _acquisition(acquisition_mode)
        )

    def get_limits(self, channels: Sequence[int], acquisition_mode: Optional[str] = None) -> CaptureLimits:
        count = max(len(list(channels)), 1)
        depth = protocol.channel_depth(self.profile, count)
        memory = (MAX_SAMPLE_BYTES // count) & ~(protocol.SAMPLES_ALIGN - 1)
        if _acquisition(acquisition_mode) == AcquisitionMode.STREAM:
            total = memory
            max_pre = depth * protocol.MAX_TRIGGER_PERCENT_STREAM // 100
        else:
            total = min(depth, memory)
            max_pre = depth * protocol.MAX_TRIGGER_PERCENT_BUFFER // 100
        return CaptureLimits(
            min_pre_samples=0,
            max_pre_samples=min(max_pre, total - 1),
            min_post_samples=1,
            max_post_samples=total,
        )

    def get_device_info(self) -> AnalyzerDeviceInfo:
        # Buffer mode limits for 8, 16 (and 24) enabled channels, as the dialog lists them
        counts = [count for count in (8, 16, 24) if count <= self.channel_count]
        return AnalyzerDeviceInfo(
            name=self.profile.model,
            max_frequency=self.max_frequency,
            blast_frequency=0,
            channels=self.channel_count,
            buffer_size=self.buffer_size,
            mode_limits=[self.get_limits(range(count)) for count in counts],
        )

    def device_details(self) -> dict[str, str]:
        speed = "USB 3 (SuperSpeed)" if self.info.super_speed else "USB 2 (High-Speed)"
        details = {
            "MODEL": self.profile.model,
            "USB": speed,
            "LOCATION": self.info.location,
            "FIRMWARE": f"{self.firmware_version[0]}.{self.firmware_version[1]}",
            "FPGA": f"0x{self.hdl_version:02X}",
            "MEMORY": f"{self.profile.hw_depth // (1 << 20)} Mbit",
        }
        if self.info.serial_number:
            details["SERIAL"] = self.info.serial_number
        return details

    def enter_bootloader(self) -> bool:
        return False

    # ------------------------------------------------------------- capture
    def trigger_spec(self, session: CaptureSession) -> Optional[TriggerSpec]:
        """The DSView simple trigger for ``session``; ``None`` when it cannot be expressed."""
        if session.trigger_type == TriggerType.IMMEDIATE:
            return protocol.NO_TRIGGER
        if session.trigger_type == TriggerType.EDGE:
            if not 0 <= session.trigger_channel < self.channel_count:
                return None  # no external trigger input
            return TriggerSpec({session.trigger_channel: "F" if session.trigger_inverted else "R"})
        if session.trigger_type in (TriggerType.COMPLEX, TriggerType.FAST):
            first = session.trigger_channel
            bits = session.trigger_bit_count
            if not (1 <= bits and 0 <= first and first + bits <= self.channel_count):
                return None
            return TriggerSpec({
                first + bit: "1" if session.trigger_pattern & (1 << bit) else "0" for bit in range(bits)
            })
        return None

    def capture_setup(self, session: CaptureSession) -> Optional[CaptureSetup]:
        """Validates ``session``; ``None`` when the device cannot capture it."""
        channels = tuple(sorted(set(session.channel_numbers)))
        if not channels or channels[0] < 0 or channels[-1] >= self.channel_count:
            return None
        if session.loop_count != 0:
            return None
        trigger = self.trigger_spec(session)
        if trigger is None:
            return None
        mode = _acquisition(session.acquisition_mode)
        if session.frequency not in self.sample_rates(channels, session.acquisition_mode):
            return None

        pre = 0 if session.trigger_type == TriggerType.IMMEDIATE else session.pre_trigger_samples
        total = pre + session.post_trigger_samples
        limits = self.get_limits(channels, session.acquisition_mode)
        if not (
            0 <= pre <= limits.max_pre_samples
            and session.post_trigger_samples >= 1
            and total <= limits.max_total_samples
        ):
            return None
        return CaptureSetup(
            profile=self.profile,
            super_speed=self.info.super_speed,
            channels=channels,
            rate=session.frequency,
            samples=total,
            pre_trigger=pre,
            mode=mode,
            trigger=trigger,
        )

    def start_capture(
        self, session: CaptureSession, completed_handler: Optional[CaptureCompletedHandler] = None
    ) -> CaptureError:
        with self._lock:
            if self._capturing:
                return CaptureError.BUSY
            setup = self.capture_setup(session)
            if setup is None:
                log.debug("Capture rejected: invalid settings")
                return CaptureError.BAD_PARAMS

            try:
                if session.threshold_voltage is not None:
                    self.set_threshold(session.threshold_voltage)
                self._write(protocol.CTL_STOP)
                self._drain_input()
                self._arm(setup)
            except (UsbError, DeviceConnectionError) as error:
                log.debug("Error arming the capture: %s", error)
                return CaptureError.HARDWARE_ERROR

            self._abort.clear()
            self._capturing = True
            self._capture_thread = threading.Thread(
                target=self._read_capture,
                args=(session, setup, completed_handler),
                name="pipilogicanalyzer-dslogic-capture",
                daemon=True,
            )
            self._capture_thread.start()
            try:
                self._write(protocol.CTL_START)
            except UsbError as error:
                log.debug("Error starting the capture: %s", error)
                self._capturing = False
                self._abort.set()
                return CaptureError.HARDWARE_ERROR
            log.debug(
                "Capture started: %s, %d Hz, channels %s, %d samples",
                setup.mode.value, setup.rate, list(setup.channels), setup.actual_samples,
            )
            return CaptureError.NONE

    def _arm(self, setup: CaptureSetup) -> None:
        """``dsl_fpga_arm``: sends the capture settings to the FPGA."""
        settings = protocol.build_settings(setup)
        if not setup.super_speed:
            self._write(protocol.CTL_WORDWIDE, bytes([protocol.WR_WORDWIDE]))
        self._write(protocol.CTL_BULK_WR, protocol.length24(len(settings) // 2))
        self._wait_status(protocol.SYS_CLR, "that it is ready for the settings")
        self._device.bulk_write(protocol.EP_OUT, settings)
        if self.profile.channels > 16:
            self._device.bulk_write(protocol.EP_OUT, protocol.build_settings_ext32(setup))
        self._write(protocol.CTL_INTRDY, bytes([protocol.WR_INTRDY]))
        if not self._status() & protocol.GPIF_DONE:
            raise DeviceConnectionError("The DSLogic did not accept the capture settings.")

    def _read_some(self, length: int, stall_timeout: Optional[float]) -> Optional[bytes]:
        """One bulk read; ``None`` on abort.

        Without ``stall_timeout`` it waits as long as needed: a buffer capture sends nothing until
        the trigger fired and the memory is full.
        """
        started = time.monotonic()
        while not self._abort.is_set():
            try:
                data = self._device.bulk_read(protocol.EP_IN, length, READ_SLICE_MS)
            except UsbTimeout:
                if stall_timeout is not None and time.monotonic() - started > stall_timeout:
                    raise UsbError("the DSLogic stopped sending data (USB too slow?)") from None
                continue
            if data:
                return data
        return None

    def _read_capture(
        self,
        session: CaptureSession,
        setup: CaptureSetup,
        completed_handler: Optional[CaptureCompletedHandler],
    ) -> None:
        stream = setup.mode == AcquisitionMode.STREAM
        # Packets are 512 bytes (USB 2) or 1024 bytes (USB 3): read whole packets only.
        packet = 1024 if setup.super_speed else 512
        try:
            header_data = self._read_some(self.profile.header_size, None)
            if header_data is None:
                return self._finish_aborted()
            header = protocol.parse_header(header_data)
            if len(header_data) != self.profile.header_size or not header.valid:
                raise UsbError("invalid trigger header from the DSLogic")

            expected = protocol.captured_bytes(setup, header)
            buffer = np.empty(expected, dtype=np.uint8)
            received = 0
            chunk = protocol.transfer_size(setup)
            while received < expected:
                remaining = (expected - received + packet - 1) // packet * packet
                data = self._read_some(min(chunk, remaining), STREAM_STALL_TIMEOUT if stream else None)
                if data is None:
                    return self._finish_aborted()
                take = min(len(data), expected - received)
                buffer[received : received + take] = np.frombuffer(data, dtype=np.uint8, count=take)
                received += take

            self._stop_device()
            self._store_samples(session, setup, header, buffer.tobytes())
            self._capturing = False
            log.debug("Capture complete: %d bytes, trigger at %d", received, header.real_pos)
            self._raise_capture_completed(CaptureCompletedArgs(success=True, session=session), completed_handler)
        except Exception as error:  # noqa: BLE001 - reported to the UI
            if self._abort.is_set():
                return self._finish_aborted()
            log.debug("Error reading the capture: %s", error)
            self._stop_device()
            self._capturing = False
            self._raise_capture_completed(
                CaptureCompletedArgs(success=False, session=session, error=str(error)), completed_handler
            )

    def _store_samples(
        self, session: CaptureSession, setup: CaptureSetup, header: protocol.TriggerHeader, data: bytes
    ) -> None:
        samples = protocol.deinterleave(data, setup.channels)
        total = len(next(iter(samples.values()))) if samples else 0
        for channel in session.capture_channels:
            channel.samples = samples.get(channel.channel_number, np.zeros(total, dtype=np.uint8))
        if setup.trigger.enabled:
            pre = min(header.real_pos, total)
        else:
            pre = 0
        session.pre_trigger_samples = pre
        session.post_trigger_samples = total - pre
        session.loop_count = 0
        session.bursts = None

    def _stop_device(self) -> None:
        try:
            self._write(protocol.CTL_STOP)
        except Exception as error:  # noqa: BLE001 - the device may be gone
            log.debug("Stop command failed: %s", error)

    def _finish_aborted(self) -> None:
        self._stop_device()
        self._capturing = False

    def stop_capture(self) -> bool:
        if not self._capturing:
            return False
        self._abort.set()
        try:
            self.write_register(protocol.CTR0_ADDR, protocol.CTR0_FORCE_RDY)
        except Exception as error:  # noqa: BLE001 - the device may be gone
            log.debug("Abort command failed: %s", error)
        thread = self._capture_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._capturing = False
        return True

    # -------------------------------------------------------------- cleanup
    def dispose(self) -> None:
        if self._capturing:
            self.stop_capture()
        try:
            self._device.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        super().dispose()
