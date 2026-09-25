# Copyright (C) 2026 Julian Decker
# Copyright (C) DreamSourceLab, DSView (libsigrok4DSL/hardware/DSL)
#
# Part of PiPiLogicAnalyzer. The protocol follows the DSLogic driver of DSView (GPL-3.0-or-later),
# https://github.com/DreamSourceLab/DSView, files libsigrok4DSL/hardware/DSL/dsl.h, dsl.c,
# dslogic.c, command.h and libsigrok4DSL/trigger.c.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Host protocol of the DreamSourceLab DSLogic analyzers (DSView "v2" firmware).

Pure functions and tables without any USB access, so that everything sent to the device can be
tested byte by byte. :mod:`.driver` puts them on the wire.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

import numpy as np

VENDOR_ID = 0x2A0E
MANUFACTURER = "DreamSourceLab"
#: iProduct of a board running the firmware this driver speaks to
PRODUCT_V2 = "USB-based DSL Instrument v2"
REQUIRED_FIRMWARE_MAJOR = 2
HDL_VERSION = 0x0E

# ---------------------------------------------------------------- USB layout
USB_INTERFACE = 0
USB_CONFIGURATION = 1
EP_OUT = 0x02
EP_IN = 0x86

# ------------------------------------------------------ vendor requests (command.h)
CMD_CTL_WR = 0xB0
CMD_CTL_RD_PRE = 0xB1
CMD_CTL_RD = 0xB2
REQUEST_TYPE_OUT = 0x40  # vendor, host to device
REQUEST_TYPE_IN = 0xC0  # vendor, device to host
#: The ``ctl_wr_cmd`` of DSView carries at most 60 bytes of data
MAX_WRITE_DATA = 60

CTL_FW_VERSION = 0
CTL_REVID_VERSION = 1
CTL_HW_STATUS = 2
CTL_PROG_B = 3
CTL_SYS = 4
CTL_LED = 5
CTL_INTRDY = 6
CTL_WORDWIDE = 7
CTL_START = 8
CTL_STOP = 9
CTL_BULK_WR = 10
CTL_REG = 11
CTL_NVM = 12
CTL_I2C_REG = 14
CTL_I2C_STATUS = 15

# HW_STATUS bits
GPIF_DONE = 1 << 7
FPGA_DONE = 1 << 6
FPGA_INIT_B = 1 << 5
SYS_OVERFLOW = 1 << 4
SYS_CLR = 1 << 3
LED_RED = 1 << 1
LED_GREEN = 1 << 0

WR_PROG_B = 1 << 2
WR_INTRDY = 1 << 7
WR_WORDWIDE = 1 << 0

# FPGA registers
VTH_ADDR = 0x78
CTR0_ADDR = 0x70
ADCC_ADDR = 0x48
HDL_VERSION_ADDR = 0x04
SEC_CTRL_ADDR = 0x73
SEC_DATA_ADDR = 0x75

# CTR0 bits
CTR0_NONE = 0
CTR0_FORCE_RDY = 1 << 1
CTR0_FORCE_STOP = 1 << 2

# Settings "mode" bits (dsl.h)
TRIG_EN_BIT = 0
HALF_MODE_BIT = 5
QUAR_MODE_BIT = 6
SLOW_ACQ_BIT = 10
STREAM_MODE_BIT = 12
#: The FPGA captures a counter instead of the inputs: sample n holds n on channels 0..15
INT_TEST_BIT = 15

# Security handshake of the boards with CAPS_FEATURE_SECURITY (command.h): the FPGA only
# captures after it received the key words stored in the EEPROM of the board.
SECU_READY = 1 << 3
SECU_PASS = 1 << 4
SECU_STEPS = 8
SECU_START = 0x0513
SECU_CHECK = 0x0219
SECU_EEP_ADDR = 0x3C00
SECU_TRY_COUNT = 8

TRIG_CHECKID = 0x55555555
NUM_TRIGGER_STAGES = 16
TRIGGER_PROBES = 16
#: Samples are transferred and counted in blocks of 64 per channel
ATOMIC_SAMPLES = 64
#: Sample counts are aligned to 1024 (SAMPLES_ALIGN)
SAMPLES_ALIGN = 1024
#: Largest capture the FPGA counts (``cnt``: 32 bits of 16 samples), used by endless streams
STREAM_MAX_SAMPLES = (0xFFFFFFFF << 4) & ~(SAMPLES_ALIGN - 1)
#: Largest pre-trigger share in buffer mode (DS_MAX_TRIG_PERCENT) and in stream mode
MAX_TRIGGER_PERCENT_BUFFER = 90
MAX_TRIGGER_PERCENT_STREAM = 10

#: ``adc_clk_init_500m``: sets up the 500 MHz sampling clock (ADF4360) of the U2Pro16 and U3 models.
#: (register, bytes written one by one, delay in ms before the entry)
ADC_CLOCK_INIT = (
    (ADCC_ADDR + 2, (0x01,), 0),
    (ADCC_ADDR, (0x01, 0x61, 0x00, 0x30), 0),
    (ADCC_ADDR, (0x01, 0x40, 0xF1, 0x46), 0),
    (ADCC_ADDR, (0x01, 0x62, 0x3D, 0x40), 10),
)

# ------------------------------------------------------------- sample rates
_BASE_RATES = (
    10, 20, 50, 100, 200, 500,
    1_000, 2_000, 5_000, 10_000, 20_000, 40_000, 50_000, 100_000, 200_000, 400_000, 500_000,
    1_000_000, 2_000_000, 4_000_000, 5_000_000, 10_000_000, 20_000_000, 25_000_000, 50_000_000,
    100_000_000,
)
SAMPLE_RATES_400 = _BASE_RATES + (200_000_000, 400_000_000)
SAMPLE_RATES_1000 = _BASE_RATES + (125_000_000, 250_000_000, 500_000_000, 1_000_000_000)


class AcquisitionMode(str, Enum):
    """Buffer: into the memory of the device, then uploaded. Stream: continuously over USB."""

    BUFFER = "buffer"
    STREAM = "stream"


@dataclass(frozen=True)
class ChannelMode:
    """An entry of ``channel_modes[]``: which channels may be used up to which rate."""

    stream: bool
    #: channels 0..num-1 are available
    num: int
    #: at most this many of them may be enabled
    valid: int
    min_rate: int
    max_rate: int
    hw_max_rate: int
    pre_div: int

    def allows(self, channels: Sequence[int]) -> bool:
        return bool(channels) and max(channels) < self.num and len(channels) <= self.valid


def _plus(stream: bool, num: int, valid: int, max_rate: int) -> ChannelMode:
    return ChannelMode(stream, num, valid, 50_000, max_rate, 100_000_000, 1)


def _u3(stream: bool, num: int, valid: int, min_rate: int, max_rate: int) -> ChannelMode:
    return ChannelMode(stream, num, valid, min_rate, max_rate, 500_000_000, 5)


PLUS_MODES = (
    _plus(True, 16, 16, 20_000_000),
    _plus(True, 16, 12, 25_000_000),
    _plus(True, 16, 6, 50_000_000),
    _plus(True, 16, 3, 100_000_000),
    _plus(False, 16, 16, 100_000_000),
    _plus(False, 8, 8, 200_000_000),
    _plus(False, 4, 4, 400_000_000),
)
U3_BUFFER_16 = (
    _u3(False, 16, 16, 1_000_000, 500_000_000),
    _u3(False, 8, 8, 1_000_000, 1_000_000_000),
)
U3PRO16_HIGH = (
    _u3(True, 16, 16, 100_000, 20_000_000),
    _u3(True, 16, 12, 100_000, 25_000_000),
    _u3(True, 16, 6, 100_000, 50_000_000),
    _u3(True, 16, 3, 100_000, 100_000_000),
) + U3_BUFFER_16
U3PRO16_SUPER = (
    _u3(True, 16, 16, 1_000_000, 125_000_000),
    _u3(True, 16, 12, 1_000_000, 250_000_000),
    _u3(True, 16, 6, 1_000_000, 500_000_000),
    _u3(True, 8, 3, 1_000_000, 1_000_000_000),
) + U3_BUFFER_16
U3PRO32_HIGH = (
    _u3(True, 32, 32, 100_000, 10_000_000),
    _u3(True, 32, 16, 100_000, 20_000_000),
    _u3(True, 32, 12, 100_000, 25_000_000),
    _u3(True, 32, 6, 100_000, 50_000_000),
    _u3(True, 32, 3, 100_000, 100_000_000),
    _u3(False, 32, 32, 1_000_000, 250_000_000),
) + U3_BUFFER_16
U3PRO32_SUPER = (
    _u3(True, 32, 32, 1_000_000, 50_000_000),
    _u3(True, 32, 30, 1_000_000, 100_000_000),
    _u3(True, 32, 12, 1_000_000, 250_000_000),
    _u3(True, 16, 6, 1_000_000, 500_000_000),
    _u3(True, 8, 3, 1_000_000, 1_000_000_000),
    _u3(False, 32, 32, 1_000_000, 250_000_000),
) + U3_BUFFER_16


@dataclass(frozen=True)
class Profile:
    """A supported model (``DSL_profile`` of DSView)."""

    pid: int
    model: str
    #: firmware image of the FX2 (uploaded only when the board does not run it yet)
    firmware: str
    bitstream: str
    channels: int
    #: sample memory in bits, shared by the enabled channels
    hw_depth: int
    rates: tuple[int, ...]
    half_rate: int
    quarter_rate: int
    #: USB 3 models: FX3, 1 kB header
    usb3: bool
    #: threshold DAC scaled for 2.5 V instead of 3.3 V (Plus with Pango FPGA)
    max25_vth: bool = False
    #: 500 MHz / 1 GHz sampling clock from an ADF4360 (U2Pro16, U3Pro16, U3Pro32)
    adf4360: bool = False
    #: the FPGA needs the security handshake before it captures
    security: bool = False
    modes_high: tuple[ChannelMode, ...] = ()
    modes_super: tuple[ChannelMode, ...] = ()

    def modes(self, super_speed: bool) -> tuple[ChannelMode, ...]:
        return self.modes_super if super_speed and self.modes_super else self.modes_high

    @property
    def header_size(self) -> int:
        return 1024 if self.usb3 else 512


PROFILES: dict[int, Profile] = {
    0x0020: Profile(0x0020, "DSLogic Plus", "DSLogicPlus.fw", "DSLogicPlus.bin", 16, 256 << 20,
                    SAMPLE_RATES_400, 200_000_000, 400_000_000, False, modes_high=PLUS_MODES),
    0x0030: Profile(0x0030, "DSLogic Plus", "DSLogicPlus.fw", "DSLogicPlus-pgl12.bin", 16, 256 << 20,
                    SAMPLE_RATES_400, 200_000_000, 400_000_000, False, max25_vth=True,
                    security=True, modes_high=PLUS_MODES),
    0x0034: Profile(0x0034, "DSLogic Plus", "DSLogicPlus-pgl12-2.fw", "DSLogicPlus-pgl12-2.bin", 16,
                    256 << 20, SAMPLE_RATES_400, 200_000_000, 400_000_000, False, max25_vth=True,
                    security=True, modes_high=PLUS_MODES),
    0x002A: Profile(0x002A, "DSLogic U3Pro16", "DSLogicU3Pro16.fw", "DSLogicU3Pro16.bin", 16, 2 << 30,
                    SAMPLE_RATES_1000, 500_000_000, 1_000_000_000, True, adf4360=True,
                    modes_high=U3PRO16_HIGH, modes_super=U3PRO16_SUPER),
    0x002C: Profile(0x002C, "DSLogic U3Pro32", "DSLogicU3Pro32.fw", "DSLogicU3Pro32.bin", 32, 2 << 30,
                    SAMPLE_RATES_1000, 500_000_000, 1_000_000_000, True, adf4360=True,
                    modes_high=U3PRO32_HIGH, modes_super=U3PRO32_SUPER),
    # The U3Pro16 electronics on an FX2: USB 2 only, 4 GB memory
    0x002D: Profile(0x002D, "DSLogic U2Pro16", "DSLogicU2Pro16.fw", "DSLogicU2Pro16.bin", 16, 4 << 30,
                    SAMPLE_RATES_1000, 500_000_000, 1_000_000_000, False, adf4360=True,
                    security=True, modes_high=U3PRO16_HIGH),
}


# ----------------------------------------------------------- control transfers
def ctl_header(dest: int, size: int, offset: int = 0) -> bytes:
    """``struct ctl_header`` (packed): dest, offset (u16), size."""
    return struct.pack("<BHB", dest, offset, size)


def ctl_write(dest: int, data: bytes = b"", offset: int = 0) -> bytes:
    """Payload of a ``CMD_CTL_WR`` request."""
    if len(data) > MAX_WRITE_DATA:
        raise ValueError(f"at most {MAX_WRITE_DATA} bytes per control write")
    return ctl_header(dest, len(data), offset) + bytes(data)


def length24(value: int) -> bytes:
    """24 bit little-endian length of ``DSL_CTL_BULK_WR``."""
    if not 0 <= value < 1 << 24:
        raise ValueError("length does not fit into 24 bits")
    return value.to_bytes(3, "little")


def security_key(eeprom: bytes) -> tuple[int, ...]:
    """The key words of the security handshake from the EEPROM at ``SECU_EEP_ADDR``."""
    return struct.unpack(f"<{SECU_STEPS}H", eeprom[: 2 * SECU_STEPS])


def threshold_code(voltage: float, max25_vth: bool) -> int:
    """Value of the threshold DAC (``VTH_ADDR``) for ``voltage`` (dslogic.c ``dev_open``)."""
    scale = 1.0 / 2.0 if max25_vth else 1.5 / 2.5
    return max(0, min(255, int(voltage / 3.3 * scale * 255)))


# --------------------------------------------------------------- capture setup
@dataclass(frozen=True)
class TriggerSpec:
    """Simple trigger of DSView (stage 0), per channel one of ``X 0 1 R F C``."""

    conditions: dict[int, str]

    @property
    def enabled(self) -> bool:
        return any(value != "X" for value in self.conditions.values())


NO_TRIGGER = TriggerSpec({})


@dataclass(frozen=True)
class CaptureSetup:
    """Everything :func:`build_settings` needs."""

    profile: Profile
    super_speed: bool
    channels: tuple[int, ...]
    rate: int
    #: requested samples per channel (before alignment)
    samples: int
    #: samples before the trigger
    pre_trigger: int
    mode: AcquisitionMode
    trigger: TriggerSpec = NO_TRIGGER
    #: capture the internal test counter (self-test)
    internal_test: bool = False
    #: endless stream: keep only the latest samples (a multiple of 64), 0 keeps everything
    keep_samples: int = 0

    @property
    def channel_count(self) -> int:
        return max(len(self.channels), 1)

    @property
    def actual_samples(self) -> int:
        """Samples the device captures: rounded up to 1024."""
        return (self.samples + SAMPLES_ALIGN - 1) // SAMPLES_ALIGN * SAMPLES_ALIGN

    @property
    def actual_bytes(self) -> int:
        return self.actual_samples // ATOMIC_SAMPLES * self.channel_count * (ATOMIC_SAMPLES // 8)

    @property
    def bytes_per_ms(self) -> int:
        return math.ceil(self.rate / 1000 * self.channel_count / 8)

    @property
    def channel_mode(self) -> ChannelMode:
        mode = select_channel_mode(self.profile, self.super_speed, self.channels, self.rate, self.mode)
        if mode is None:
            raise ValueError("no channel mode of the device allows these channels at this rate")
        return mode


def channel_depth(profile: Profile, channel_count: int) -> int:
    """Samples per channel the device memory holds (``dsl_channel_depth``)."""
    return (profile.hw_depth // max(channel_count, 1)) & ~(SAMPLES_ALIGN - 1)


def select_channel_mode(
    profile: Profile, super_speed: bool, channels: Sequence[int], rate: int, mode: AcquisitionMode
) -> Optional[ChannelMode]:
    """The channel mode used for ``channels`` at ``rate``; DSView lets the user pick it."""
    stream = mode == AcquisitionMode.STREAM
    for candidate in profile.modes(super_speed):
        if (
            candidate.stream == stream
            and candidate.allows(channels)
            and candidate.min_rate <= rate <= candidate.max_rate
        ):
            return candidate
    return None


def max_rate(profile: Profile, super_speed: bool, channels: Sequence[int], mode: AcquisitionMode) -> int:
    """Highest rate any channel mode allows for ``channels`` (0: the channels are not allowed)."""
    stream = mode == AcquisitionMode.STREAM
    rates = [
        candidate.max_rate
        for candidate in profile.modes(super_speed)
        if candidate.stream == stream and candidate.allows(channels)
    ]
    return max(rates, default=0)


def available_rates(
    profile: Profile, super_speed: bool, channels: Sequence[int], mode: AcquisitionMode
) -> list[int]:
    """Rates of the device usable for ``channels`` in ``mode``."""
    return [
        rate
        for rate in profile.rates
        if select_channel_mode(profile, super_speed, channels, rate, mode) is not None
    ]


def divider(channel_mode: ChannelMode, rate: int) -> tuple[int, int]:
    """``div_l``, ``div_h`` of the settings (``dsl_fpga_arm``, LOGIC mode)."""
    ratio = math.ceil(channel_mode.hw_max_rate / rate)
    div_h = ((channel_mode.pre_div - 1) if ratio >= channel_mode.pre_div else ratio - 1) << 8
    ratio = math.ceil(ratio / channel_mode.pre_div)
    return ratio & 0xFFFF, div_h + (ratio >> 16)


def trigger_position(setup: CaptureSetup) -> int:
    """Trigger position sent to the device: at least 64, limited, aligned to 64 samples."""
    depth = channel_depth(setup.profile, setup.channel_count)
    percent = (
        MAX_TRIGGER_PERCENT_STREAM if setup.mode == AcquisitionMode.STREAM else MAX_TRIGGER_PERCENT_BUFFER
    )
    position = max(setup.pre_trigger, ATOMIC_SAMPLES)
    return min(position, depth * percent // 100)


def _replicate(bits: int, quarter: bool, half: bool) -> int:
    """Copies the pattern for the interleaved sample lanes of the half/quarter clock modes."""
    if quarter:
        low = bits & (0xFFFF >> (TRIGGER_PROBES - TRIGGER_PROBES // 4))
        return sum(low << (TRIGGER_PROBES // 4 * lane) for lane in range(4)) & 0xFFFF
    if half:
        low = bits & (0xFFFF >> (TRIGGER_PROBES - TRIGGER_PROBES // 2))
        return sum(low << (TRIGGER_PROBES // 2 * lane) for lane in range(2)) & 0xFFFF
    return bits


def trigger_words(trigger: TriggerSpec, first: int, quarter: bool, half: bool) -> tuple[int, int, int]:
    """mask, value, edge of stage 0 for channels ``first``..``first+15`` (``ds_trigger_get_*0``)."""
    mask = value = edge = 0
    for bit in range(TRIGGER_PROBES):
        condition = trigger.conditions.get(first + bit, "X")
        mask |= (condition in "XC") << bit
        value |= (condition in "1R") << bit
        edge |= (condition in "RFC") << bit
    return (
        _replicate(mask, quarter, half),
        _replicate(value, quarter, half),
        _replicate(edge, quarter, half),
    )


def settings_mode(setup: CaptureSetup) -> int:
    mode = (
        (setup.trigger.enabled << TRIG_EN_BIT)
        | ((setup.rate == setup.profile.half_rate) << HALF_MODE_BIT)
        | ((setup.rate == setup.profile.quarter_rate) << QUAR_MODE_BIT)
        | ((setup.bytes_per_ms < 1024) << SLOW_ACQ_BIT)
        | ((setup.mode == AcquisitionMode.STREAM) << STREAM_MODE_BIT)
        | (setup.internal_test << INT_TEST_BIT)
    )
    return mode


SETTINGS_SIZE = 372
SETTINGS_EXT32_SIZE = 204


def build_settings(setup: CaptureSetup) -> bytes:
    """``struct DSL_setting`` (372 bytes) sent before every capture."""
    channel_mode = setup.channel_mode
    mode = settings_mode(setup)
    div_l, div_h = divider(channel_mode, setup.rate)
    count = setup.actual_samples >> 4  # the "hardware minimum unit" of dsl_fpga_arm
    tpos = trigger_position(setup)
    trig_glb = ((setup.channel_count & 0x1F) << 8) | 0  # simple trigger: 0 stages beyond the first
    ch_en = sum(1 << channel for channel in setup.channels)

    quarter, half = trigger_replication(setup.profile, mode)
    mask, value, edge = trigger_words(setup.trigger, 0, quarter, half)

    trig_mask0 = [mask] + [0xFFFF] * 15
    trig_mask1 = [0xFFFF] * 16  # condition 1 is "X" everywhere in the simple trigger
    trig_value0 = [value] + [0] * 15
    trig_value1 = [0] * 16
    trig_edge0 = [edge] + [0] * 15
    trig_edge1 = [0] * 16
    # (trigger_logic << 1) + inverted; DSView's default logic is 1
    trig_logic0 = [2] * 16
    trig_logic1 = [2] * 16
    trig_count = [0] * 16

    data = struct.pack(
        "<I" + "H" * 22,
        0xF5A5F5A5,
        0x0001, mode,
        0x0102, div_l, div_h,
        0x0302, count & 0xFFFF, (count >> 16) & 0xFFFF,
        0x0502, tpos & 0xFFC0, (tpos >> 16) & 0xFFFF,
        0x0701, trig_glb,
        0x0802, setup.actual_samples & 0xFFFF, (setup.actual_samples >> 16) & 0xFFFF,
        0x0A02, ch_en & 0xFFFF, (ch_en >> 16) & 0xFFFF,
        0x0C01, 0,
        0x40A0,
    )
    data += struct.pack(
        "<" + "H" * 128,
        *trig_mask0, *trig_mask1, *trig_value0, *trig_value1,
        *trig_edge0, *trig_edge1, *trig_logic0, *trig_logic1,
    )
    data += struct.pack("<16I", *trig_count)
    data += struct.pack("<I", 0xFA5AFA5A)
    assert len(data) == SETTINGS_SIZE
    return data


def build_settings_ext32(setup: CaptureSetup) -> bytes:
    """``struct DSL_setting_ext32`` (204 bytes): the trigger of channels 16-31 (U3Pro32)."""
    quarter, half = trigger_replication(setup.profile, settings_mode(setup))
    mask, value, edge = trigger_words(setup.trigger, TRIGGER_PROBES, quarter, half)
    data = struct.pack("<IH", 0xF5A5F5A5, 0x6060)
    data += struct.pack(
        "<" + "H" * 96,
        mask, *[0xFFFF] * 15,
        *[0xFFFF] * 16,
        value, *[0] * 15,
        *[0] * 16,
        edge, *[0] * 15,
        *[0] * 16,
    )
    data += struct.pack("<HI", 0xFFFF, 0xFA5AFA5A)
    assert len(data) == SETTINGS_EXT32_SIZE
    return data


def trigger_replication(profile: Profile, mode: int) -> tuple[bool, bool]:
    """(quarter, half) replication of the trigger pattern (``dsl_fpga_arm``)."""
    quarter_mode = bool(mode & (1 << QUAR_MODE_BIT))
    half_mode = bool(mode & (1 << HALF_MODE_BIT))
    if profile.adf4360:  # ADF4360 clock: 1 GHz uses the half replication, 500 MHz none
        return False, quarter_mode
    return quarter_mode, half_mode


# ---------------------------------------------------------------- the capture
@dataclass(frozen=True)
class TriggerHeader:
    """``struct ds_trigger_pos``, the first bulk transfer of a capture."""

    check_id: int
    real_pos: int
    ram_saddr: int
    remain_count: int
    status: int

    @property
    def valid(self) -> bool:
        return self.check_id == TRIG_CHECKID

    @property
    def triggered(self) -> bool:
        return bool(self.status & 1)


def parse_header(data: bytes) -> TriggerHeader:
    if len(data) < 24:
        raise ValueError("trigger header too short")
    check_id, real_pos, ram_saddr, remain_l, remain_h, status = struct.unpack_from("<6I", data)
    return TriggerHeader(check_id, real_pos, ram_saddr, (remain_h << 32) | remain_l, status)


def captured_bytes(setup: CaptureSetup, header: TriggerHeader) -> int:
    """Bytes to read after the header: less than requested when a buffer capture was stopped early."""
    # The device counts the aligned sample number it was sent (``cnt``), so does the remainder.
    if setup.mode == AcquisitionMode.BUFFER and header.remain_count < setup.actual_samples:
        samples = (setup.actual_samples - header.remain_count) & ~(SAMPLES_ALIGN - 1)
        return samples // ATOMIC_SAMPLES * setup.channel_count * (ATOMIC_SAMPLES // 8)
    return setup.actual_bytes


def deinterleave(data: bytes, channels: Sequence[int]) -> dict[int, np.ndarray]:
    """Per-channel samples (``uint8`` 0/1) of the ``LA_CROSS_DATA`` format.

    The data is a sequence of 8 byte blocks, one per enabled channel in ascending order; each
    block holds 64 samples of its channel, bit k of the little-endian word being sample k.
    """
    count = len(channels)
    if count == 0:
        return {}
    usable = len(data) // (8 * count) * (8 * count)
    blocks = np.frombuffer(data, dtype=np.uint8, count=usable).reshape(-1, count, 8)
    bits = np.unpackbits(blocks, axis=2, bitorder="little")  # (blocks, channels, 64)
    return {channel: np.ascontiguousarray(bits[:, index, :]).reshape(-1) for index, channel in
            enumerate(sorted(channels))}


def deinterleave_into(data: bytes, channels: Sequence[int], arrays: dict[int, np.ndarray], offset: int) -> int:
    """Unpacks whole 64 sample rows of ``data`` into ``arrays`` from sample ``offset``.

    Returns the number of samples per channel written; bytes of an incomplete row are ignored.
    """
    count = len(channels)
    rows = len(data) // (8 * count) if count else 0
    if rows == 0:
        return 0
    blocks = np.frombuffer(data, dtype=np.uint8, count=rows * 8 * count).reshape(rows, count, 8)
    bits = np.unpackbits(blocks, axis=2, bitorder="little")  # (rows, channels, 64)
    written = rows * ATOMIC_SAMPLES
    for index, channel in enumerate(sorted(channels)):
        arrays[channel][offset : offset + written] = bits[:, index, :].reshape(-1)
    return written


def transfer_size(setup: CaptureSetup) -> int:
    """Size of one bulk read: about 20 ms (USB 2) or 10 ms (USB 3) of data in stream mode."""
    align = 1024 if setup.super_speed else 512
    if setup.mode == AcquisitionMode.STREAM:
        size = (10 if setup.super_speed else 20) * setup.bytes_per_ms
        # Fewer, larger reads keep the synchronous USB loop ahead of the device.
        size = max(size * 4, 64 * 1024)
    else:
        size = 1024 * 1024
    return (size + align - 1) // align * align
