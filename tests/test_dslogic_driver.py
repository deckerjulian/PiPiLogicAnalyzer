"""DSLogic driver against a simulated board (no USB hardware needed)."""

from __future__ import annotations

import struct
import threading
import time

import numpy as np
import pytest

from pipilogicanalyzer.driver.base import (
    ACQUISITION_STREAM,
    CAPABILITY_IMMEDIATE_TRIGGER,
    AnalyzerDriverType,
    CaptureError,
    DeviceConnectionError,
)
from pipilogicanalyzer.driver.dslogic import BitstreamMissingError, DSLogicDriver, protocol
from pipilogicanalyzer.driver.dslogic.driver import FirmwareMissingError, prepare
from pipilogicanalyzer.driver.dslogic.resources import ResourceError
from pipilogicanalyzer.driver.dslogic.usb import UsbDevice, UsbDeviceInfo, UsbTimeout
from pipilogicanalyzer.driver.models import AnalyzerChannel, CaptureSession, TriggerType


class FakeDSLogic(UsbDevice):
    """Answers the vendor requests like a board and serves queued bulk data."""

    def __init__(self, info: UsbDeviceInfo, fpga_done: bool = True, firmware=(2, 0), hdl=protocol.HDL_VERSION):
        self.info = info
        self.fpga_done = fpga_done
        self.firmware = firmware
        self.hdl = hdl
        self.writes: list[tuple[int, int, bytes]] = []  # (dest, offset, data)
        self.bulk_out: list[bytes] = []
        self.bulk_in: list[bytes] = []
        self.claimed = False
        self.closed = False
        self.started = threading.Event()
        self._pending_read: tuple[int, int, int] = (0, 0, 0)

    # control
    def control_out(self, request, data, timeout_ms=3000):
        dest, offset, size = struct.unpack_from("<BHB", data)
        if request == protocol.CMD_CTL_RD_PRE:
            self._pending_read = (dest, offset, size)
            return
        payload = bytes(data[4:4 + size])
        self.writes.append((dest, offset, payload))
        if dest == protocol.CTL_INTRDY and payload == bytes([protocol.WR_INTRDY]):
            self.fpga_done = True  # the bitstream (or the settings) ended
        if dest == protocol.CTL_START:
            self.started.set()

    def control_in(self, request, length, timeout_ms=3000):
        dest, offset, size = self._pending_read
        if dest == protocol.CTL_FW_VERSION:
            return bytes(self.firmware)
        if dest == protocol.CTL_HW_STATUS:
            status = protocol.FPGA_INIT_B | protocol.SYS_CLR | protocol.GPIF_DONE
            return bytes([status | (protocol.FPGA_DONE if self.fpga_done else 0)])
        if dest == protocol.CTL_I2C_STATUS:
            data = bytearray(size)
            if size > protocol.HDL_VERSION_ADDR:
                data[protocol.HDL_VERSION_ADDR] = self.hdl
            return bytes(data)
        return bytes(length)

    # bulk
    def bulk_write(self, endpoint, data, timeout_ms=1000):
        self.bulk_out.append(bytes(data))
        return len(data)

    def bulk_read(self, endpoint, length, timeout_ms):
        if not self.started.wait(timeout_ms / 1000) or not self.bulk_in:
            time.sleep(timeout_ms / 1000 / 10)
            raise UsbTimeout("timeout")
        chunk = self.bulk_in[0]
        if len(chunk) > length:
            self.bulk_in[0] = chunk[length:]
            return chunk[:length]
        self.bulk_in.pop(0)
        return chunk

    def claim(self):
        self.claimed = True

    def close(self):
        self.closed = True

    def dests(self) -> list[int]:
        return [dest for dest, _offset, _data in self.writes]


def info(pid=0x0020, super_speed=False) -> UsbDeviceInfo:
    return UsbDeviceInfo(bus=1, address=7, pid=pid, model=protocol.PROFILES[pid].model,
                         serial_number="ABC", super_speed=super_speed)


def open_driver(device: FakeDSLogic, bitstream: bytes = b"\x55" * 1000) -> DSLogicDriver:
    def load(name):
        if bitstream is None:
            raise ResourceError(name)
        return bitstream

    return DSLogicDriver(device.info, open_device=lambda _info: device, load_resource=load, read_delay=0)


def header(real_pos: int, remain: int = 0, size: int = 512, triggered: bool = True) -> bytes:
    return struct.pack("<6I", protocol.TRIG_CHECKID, real_pos, 0, remain, 0, int(triggered)) + bytes(size - 24)


def interleave(signals: dict[int, np.ndarray]) -> bytes:
    """Inverse of protocol.deinterleave: 64 sample blocks per channel, round robin."""
    channels = sorted(signals)
    blocks = len(next(iter(signals.values()))) // 64
    out = bytearray()
    for block in range(blocks):
        for channel in channels:
            bits = signals[channel][block * 64:(block + 1) * 64]
            out += np.packbits(bits.astype(np.uint8), bitorder="little").tobytes()
    return bytes(out)


def session(channels, frequency=1_000_000, pre=100, post=1900, **trigger) -> CaptureSession:
    capture = CaptureSession(frequency=frequency, pre_trigger_samples=pre, post_trigger_samples=post)
    capture.capture_channels = [AnalyzerChannel(channel_number=number) for number in channels]
    for name, value in trigger.items():
        setattr(capture, name, value)
    return capture


# ------------------------------------------------------------------- open
def test_open_with_a_configured_fpga_sets_threshold_only():
    device = FakeDSLogic(info())
    driver = open_driver(device)
    assert device.claimed
    assert protocol.CTL_PROG_B not in device.dests()
    registers = [(offset, data[0]) for dest, offset, data in device.writes if dest == protocol.CTL_I2C_REG]
    assert (protocol.CTR0_ADDR, 0) in registers
    assert (protocol.VTH_ADDR, protocol.threshold_code(1.0, False)) in registers
    assert driver.driver_type == AnalyzerDriverType.DSLOGIC
    assert driver.channel_count == 16 and driver.max_frequency == 400_000_000
    assert CAPABILITY_IMMEDIATE_TRIGGER in driver.capabilities()
    assert not driver.has_external_trigger()


def test_open_loads_the_bitstream_when_the_fpga_is_empty():
    device = FakeDSLogic(info(), fpga_done=False)
    open_driver(device, bitstream=b"\xAA" * 3000)
    assert device.bulk_out == [b"\xAA" * 3000]
    assert (protocol.CTL_BULK_WR, 0, protocol.length24(3000)) in device.writes
    assert device.dests()[0] == protocol.CTL_PROG_B


def test_u3_models_initialise_the_sampling_clock():
    device = FakeDSLogic(info(0x002C, super_speed=True))
    driver = open_driver(device)
    registers = [offset for dest, offset, _data in device.writes if dest == protocol.CTL_I2C_REG]
    assert registers.count(protocol.ADCC_ADDR) == 12 and protocol.ADCC_ADDR + 2 in registers
    assert driver.channel_count == 32 and driver.max_frequency == 1_000_000_000


def test_missing_bitstream_is_reported():
    device = FakeDSLogic(info(0x002A), fpga_done=False)
    with pytest.raises(BitstreamMissingError) as error:
        open_driver(device, bitstream=None)
    assert error.value.name == "DSLogicU3Pro16.bin"
    assert device.closed


def test_old_firmware_is_refused():
    device = FakeDSLogic(info(), firmware=(1, 0))
    with pytest.raises(DeviceConnectionError, match="firmware"):
        open_driver(device)


def test_u3_without_firmware_cannot_be_prepared():
    board = info(0x002A)
    board.firmware_ready = False
    with pytest.raises(FirmwareMissingError):
        prepare(board, list_devices=lambda: [], upload=lambda *_: None)


# ----------------------------------------------------------------- limits
def test_limits_and_rates_follow_the_channel_modes():
    driver = open_driver(FakeDSLogic(info()))
    assert driver.sample_rates(range(16))[-1] == 100_000_000
    assert driver.sample_rates(range(4))[-1] == 400_000_000
    assert driver.sample_rates(range(16), ACQUISITION_STREAM)[-1] == 20_000_000
    limits = driver.get_limits(range(16))
    assert limits.max_total_samples == (256 << 20) // 16
    assert driver.get_limits(range(16), ACQUISITION_STREAM).max_total_samples == (1 << 30) // 16


def test_invalid_sessions_are_rejected():
    driver = open_driver(FakeDSLogic(info()))
    assert driver.start_capture(session([0], frequency=1_234_567)) is CaptureError.BAD_PARAMS
    # 400 MHz needs the 4 channel mode
    assert driver.start_capture(session(range(16), frequency=400_000_000)) is CaptureError.BAD_PARAMS
    external = session([0], trigger_type=TriggerType.EDGE, trigger_channel=16)
    assert driver.start_capture(external) is CaptureError.BAD_PARAMS
    bursts = session([0], loop_count=2)
    assert driver.start_capture(bursts) is CaptureError.BAD_PARAMS


# ---------------------------------------------------------------- capture
def run_capture(driver, capture, timeout=5.0):
    done = threading.Event()
    results = []

    def handler(args):
        results.append(args)
        done.set()

    assert driver.start_capture(capture, handler) is CaptureError.NONE
    assert done.wait(timeout), "capture did not complete"
    return results[0]


def test_buffer_capture_end_to_end():
    device = FakeDSLogic(info())
    driver = open_driver(device)
    rng = np.random.default_rng(3)
    capture = session([1, 4], pre=100, post=1900, trigger_type=TriggerType.EDGE, trigger_channel=4)
    samples = 2048  # 2000 rounded up to 1024
    signals = {1: rng.integers(0, 2, samples), 4: rng.integers(0, 2, samples)}
    device.bulk_in = [header(real_pos=128), interleave(signals)]

    result = run_capture(driver, capture)
    assert result.success, result.error
    assert capture.capture_channels[0].samples.tolist() == signals[1].tolist()
    assert capture.capture_channels[1].samples.tolist() == signals[4].tolist()
    assert capture.pre_trigger_samples == 128
    assert capture.post_trigger_samples == samples - 128
    assert not driver.is_capturing

    settings = device.bulk_out[-1]
    assert len(settings) == protocol.SETTINGS_SIZE
    fields = struct.unpack_from("<I" + "H" * 22, settings)
    assert fields[18] == 0b10010  # channels 1 and 4
    assert fields[2] & (1 << protocol.TRIG_EN_BIT)
    assert device.dests()[-1] == protocol.CTL_STOP  # stopped after reading


def test_immediate_stream_capture_on_the_u3pro32():
    device = FakeDSLogic(info(0x002C, super_speed=True))
    driver = open_driver(device)
    capture = session(range(32), frequency=50_000_000, pre=0, post=4096,
                      trigger_type=TriggerType.IMMEDIATE, acquisition_mode=ACQUISITION_STREAM)
    signals = {channel: np.full(4096, channel % 2) for channel in range(32)}
    data = interleave(signals)
    device.bulk_in = [header(real_pos=0, size=1024), data[:5000], data[5000:]]

    result = run_capture(driver, capture)
    assert result.success, result.error
    assert capture.pre_trigger_samples == 0 and capture.post_trigger_samples == 4096
    assert capture.capture_channels[31].samples.tolist() == [1] * 4096
    # the ext32 block follows the settings
    assert [len(block) for block in device.bulk_out[-2:]] == [protocol.SETTINGS_SIZE, protocol.SETTINGS_EXT32_SIZE]
    mode = struct.unpack_from("<I" + "H" * 22, device.bulk_out[-2])[2]
    assert mode & (1 << protocol.STREAM_MODE_BIT) and not mode & (1 << protocol.TRIG_EN_BIT)


def test_pattern_trigger_uses_levels():
    device = FakeDSLogic(info())
    driver = open_driver(device)
    capture = session(range(8), trigger_type=TriggerType.COMPLEX, trigger_channel=2,
                      trigger_bit_count=3, trigger_pattern=0b101)
    setup = driver.capture_setup(capture)
    assert setup.trigger.conditions == {2: "1", 3: "0", 4: "1"}


def test_invalid_header_fails_the_capture():
    device = FakeDSLogic(info())
    driver = open_driver(device)
    device.bulk_in = [bytes(512)]
    result = run_capture(driver, session([0]))
    assert not result.success and "header" in result.error


def test_abort_stops_without_a_result():
    device = FakeDSLogic(info())
    driver = open_driver(device)
    results = []
    assert driver.start_capture(session([0]), results.append) is CaptureError.NONE
    time.sleep(0.1)
    assert driver.stop_capture()
    assert not driver.is_capturing
    assert results == []
    registers = [(offset, data) for dest, offset, data in device.writes if dest == protocol.CTL_I2C_REG]
    assert (protocol.CTR0_ADDR, bytes([protocol.CTR0_FORCE_RDY])) in registers
    driver.dispose()
    assert device.closed
