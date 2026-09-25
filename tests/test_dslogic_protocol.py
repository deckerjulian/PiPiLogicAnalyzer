"""DSLogic host protocol: settings bytes, trigger words, header and sample format."""

from __future__ import annotations

import struct

import numpy as np
import pytest

from pipilogicanalyzer.driver.dslogic import protocol
from pipilogicanalyzer.driver.dslogic.protocol import AcquisitionMode, CaptureSetup, TriggerSpec

PLUS = protocol.PROFILES[0x0020]
U3PRO16 = protocol.PROFILES[0x002A]
U3PRO32 = protocol.PROFILES[0x002C]


def setup(profile=PLUS, channels=(0, 1, 2, 3), rate=1_000_000, samples=10_000, pre=1_000,
          mode=AcquisitionMode.BUFFER, trigger=protocol.NO_TRIGGER, super_speed=False) -> CaptureSetup:
    return CaptureSetup(profile, super_speed, tuple(channels), rate, samples, pre, mode, trigger)


def words(data: bytes) -> tuple[int, ...]:
    return struct.unpack_from("<I" + "H" * 22, data)


def test_control_packets_match_ctl_header():
    assert protocol.ctl_header(protocol.CTL_HW_STATUS, 1) == bytes([2, 0, 0, 1])
    assert protocol.ctl_write(protocol.CTL_I2C_REG, b"\x05", offset=0x70) == bytes([14, 0x70, 0, 1, 5])
    assert protocol.length24(341160) == bytes([0xA8, 0x34, 0x05])
    with pytest.raises(ValueError):
        protocol.ctl_write(protocol.CTL_REG, bytes(61))


def test_threshold_codes_follow_dsview():
    assert protocol.threshold_code(1.0, max25_vth=False) == int(1.0 / 3.3 * 0.6 * 255)
    assert protocol.threshold_code(1.0, max25_vth=True) == int(1.0 / 3.3 * 0.5 * 255)
    assert protocol.threshold_code(10.0, max25_vth=False) == 255


def test_divider_of_the_plus_and_the_u3_models():
    buffer16 = protocol.select_channel_mode(PLUS, False, range(16), 1_000_000, AcquisitionMode.BUFFER)
    assert protocol.divider(buffer16, 1_000_000) == (100, 0)
    assert protocol.divider(buffer16, 400_000_000) == (1, 0)
    u3 = protocol.select_channel_mode(U3PRO16, True, range(8), 1_000_000, AcquisitionMode.BUFFER)
    # N = 500, pre_div 5: div_h = 4 << 8, div_l = 100
    assert protocol.divider(u3, 1_000_000) == (100, 0x0400)
    assert protocol.divider(u3, 1_000_000_000) == (1, 0)


def test_channel_modes_limit_the_rate():
    assert protocol.max_rate(PLUS, False, range(16), AcquisitionMode.BUFFER) == 100_000_000
    assert protocol.max_rate(PLUS, False, range(8), AcquisitionMode.BUFFER) == 200_000_000
    assert protocol.max_rate(PLUS, False, [0, 3], AcquisitionMode.BUFFER) == 400_000_000
    # channel 4 is outside of the 4 channel mode
    assert protocol.max_rate(PLUS, False, [4], AcquisitionMode.BUFFER) == 200_000_000
    assert protocol.max_rate(PLUS, False, range(16), AcquisitionMode.STREAM) == 20_000_000
    assert protocol.max_rate(U3PRO32, True, range(32), AcquisitionMode.BUFFER) == 250_000_000
    assert protocol.max_rate(U3PRO32, True, range(32), AcquisitionMode.STREAM) == 50_000_000
    assert protocol.max_rate(U3PRO32, False, range(32), AcquisitionMode.STREAM) == 10_000_000
    assert protocol.max_rate(U3PRO16, True, range(3), AcquisitionMode.STREAM) == 1_000_000_000
    rates = protocol.available_rates(PLUS, False, range(16), AcquisitionMode.BUFFER)
    assert rates[0] == 50_000 and rates[-1] == 100_000_000


def test_settings_layout_and_fields():
    capture = setup(samples=10_000, pre=1_000)
    data = protocol.build_settings(capture)
    assert len(data) == protocol.SETTINGS_SIZE
    fields = words(data)
    assert fields[0] == 0xF5A5F5A5
    mode = fields[2]
    assert mode & (1 << protocol.SLOW_ACQ_BIT)  # 1 MHz x 4 channels = 500 bytes/ms
    assert not mode & (1 << protocol.TRIG_EN_BIT)
    assert fields[4:6] == (100, 0)  # divider
    actual = 10_240
    assert capture.actual_samples == actual
    assert fields[7] | fields[8] << 16 == actual >> 4
    assert fields[10] == 1_000 & 0xFFC0  # trigger position
    assert fields[13] == 4 << 8  # trig_glb: channel count
    assert fields[15] | fields[16] << 16 == actual  # dso_cnt
    assert fields[18:20] == (0x000F, 0)  # channel enable
    assert fields[22] == 0x40A0
    assert struct.unpack_from("<I", data, 368)[0] == 0xFA5AFA5A


def test_trigger_words_and_replication():
    trigger = TriggerSpec({0: "R", 2: "1", 3: "0", 5: "F"})
    mask, value, edge = protocol.trigger_words(trigger, 0, False, False)
    assert mask == 0xFFFF & ~0b101101
    assert value == 0b000101
    assert edge == 0b100001
    # quarter mode: the low 4 bits are copied into every nibble
    mask, value, edge = protocol.trigger_words(TriggerSpec({1: "1"}), 0, True, False)
    assert value == 0x2222
    mask, value, edge = protocol.trigger_words(TriggerSpec({1: "1"}), 0, False, True)
    assert value == 0x0202


def test_trigger_replication_follows_the_sampling_clock():
    half = 1 << protocol.HALF_MODE_BIT
    quarter = 1 << protocol.QUAR_MODE_BIT
    assert protocol.trigger_replication(PLUS, quarter) == (True, False)
    assert protocol.trigger_replication(PLUS, half) == (False, True)
    # ADF4360 boards, also the U2Pro16 on USB 2: 1 GHz uses the half replication
    for profile in (U3PRO16, protocol.PROFILES[0x002D]):
        assert protocol.trigger_replication(profile, quarter) == (False, True)
        assert protocol.trigger_replication(profile, half) == (False, False)


def test_security_key_from_the_eeprom():
    eeprom = bytes.fromhex("b543b62eca373e265b3d72cf325c6e0f")
    assert protocol.security_key(eeprom) == (
        0x43B5, 0x2EB6, 0x37CA, 0x263E, 0x3D5B, 0xCF72, 0x5C32, 0x0F6E,
    )


def test_trigger_in_the_settings():
    capture = setup(trigger=TriggerSpec({1: "R"}), rate=400_000_000, channels=(0, 1))
    fields = words(protocol.build_settings(capture))
    assert fields[2] & (1 << protocol.TRIG_EN_BIT)
    assert fields[2] & (1 << protocol.QUAR_MODE_BIT)
    masks = struct.unpack_from("<16H", protocol.build_settings(capture), 48)
    assert masks[0] == 0xFFFF & ~0x2222 and masks[1] == 0xFFFF
    # The U3 models replicate by two at 1 GHz and not at all at 500 MHz
    assert protocol.trigger_replication(U3PRO16, 1 << protocol.QUAR_MODE_BIT) == (False, True)
    assert protocol.trigger_replication(U3PRO16, 1 << protocol.HALF_MODE_BIT) == (False, False)


def test_ext32_settings_carry_channels_16_to_31():
    capture = setup(U3PRO32, channels=range(32), rate=100_000_000, trigger=TriggerSpec({17: "1"}),
                    super_speed=True)
    data = protocol.build_settings_ext32(capture)
    assert len(data) == protocol.SETTINGS_EXT32_SIZE
    assert struct.unpack_from("<IH", data) == (0xF5A5F5A5, 0x6060)
    mask0 = struct.unpack_from("<H", data, 6)[0]
    value0 = struct.unpack_from("<H", data, 6 + 64)[0]
    assert mask0 == 0xFFFF & ~0b10 and value0 == 0b10
    assert struct.unpack_from("<I", data, 200)[0] == 0xFA5AFA5A


def test_trigger_position_limits():
    assert protocol.trigger_position(setup(pre=0)) == 64
    stream = setup(pre=10**9, mode=AcquisitionMode.STREAM, rate=1_000_000)
    depth = protocol.channel_depth(PLUS, 4)
    assert protocol.trigger_position(stream) == depth * 10 // 100


def test_header_and_early_stop():
    raw = struct.pack("<6I", protocol.TRIG_CHECKID, 5000, 0, 2048, 0, 1) + bytes(488)
    header = protocol.parse_header(raw)
    assert header.valid and header.triggered and header.real_pos == 5000
    capture = setup(samples=10_000)  # 10240 after alignment
    # 2048 samples were not captured: 10240 - 2048 = 8192
    assert protocol.captured_bytes(capture, header) == 8192 // 64 * 4 * 8
    complete = protocol.parse_header(struct.pack("<6I", protocol.TRIG_CHECKID, 0, 0, 0, 0, 1))
    assert protocol.captured_bytes(capture, complete) == capture.actual_bytes


def test_deinterleave_matches_a_reference_loop():
    rng = np.random.default_rng(1)
    channels = [0, 3, 7]
    data = rng.integers(0, 256, size=3 * 8 * 5, dtype=np.uint8).tobytes()
    result = protocol.deinterleave(data, channels)

    expected = {channel: [] for channel in channels}
    for block in range(5):
        for index, channel in enumerate(channels):
            word = int.from_bytes(data[(block * 3 + index) * 8:(block * 3 + index + 1) * 8], "little")
            expected[channel].extend((word >> bit) & 1 for bit in range(64))
    for channel in channels:
        assert result[channel].dtype == np.uint8
        assert result[channel].tolist() == expected[channel]
