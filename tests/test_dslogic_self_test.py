"""Board self-test of the DSLogic, against a simulated device."""

from __future__ import annotations

import numpy as np

from pipilogicanalyzer.driver.base import CaptureCompletedArgs, CaptureError, SelfTestResult
from pipilogicanalyzer.driver.dslogic import protocol, self_test
from pipilogicanalyzer.driver.dslogic.usb import UsbDeviceInfo
from pipilogicanalyzer.driver.models import TriggerType


class SimulatedDSLogic:
    """Captures like the board: the test counter or quiet inputs, triggers on the counter."""

    def __init__(self, pid=0x002D, super_speed=False, security_passed=True):
        self.profile = protocol.PROFILES[pid]
        self.info = UsbDeviceInfo(bus=1, address=1, pid=pid, model=self.profile.model, super_speed=super_speed)
        self.channel_count = self.profile.channels
        self.firmware_version = (2, 2)
        self.hdl_version = protocol.HDL_VERSION
        self.security_passed = security_passed if self.profile.security else None
        self.is_capturing = False
        self.inputs: dict[int, np.ndarray] = {}
        #: bit of the counter that the "hardware" loses (always reads 0)
        self.broken_bit = None
        self.sessions = []

    def read_status(self):
        return protocol.FPGA_DONE

    def stop_capture(self):
        return False

    def start_capture(self, session, completed, internal_test=False):
        self.sessions.append((session, internal_test))
        total = session.pre_trigger_samples + session.post_trigger_samples
        counter = np.arange(total, dtype=np.uint32)
        if self.broken_bit is not None:
            counter &= ~np.uint32(1 << self.broken_bit)
        position = 0
        if session.trigger_type == TriggerType.COMPLEX:
            mask = (1 << session.trigger_bit_count) - 1
            position = int(np.argmax((counter >> session.trigger_channel & mask) == session.trigger_pattern))
        elif session.trigger_type == TriggerType.EDGE:
            bit = counter >> session.trigger_channel & 1
            position = int(np.argmax((bit[1:] == 1) & (bit[:-1] == 0))) + 1
        for channel in session.capture_channels:
            number = channel.channel_number
            if internal_test:
                channel.samples = ((counter >> number) & 1).astype(np.uint8)
            else:
                channel.samples = self.inputs.get(number, np.zeros(total, dtype=np.uint8))
        session.pre_trigger_samples = position
        session.post_trigger_samples = total - position
        completed(CaptureCompletedArgs(success=True, session=session))
        return CaptureError.NONE


def by_item(results: list[SelfTestResult]) -> dict[str, SelfTestResult]:
    return {result.item: result for result in results}


def test_a_healthy_u2pro16_passes_every_check():
    device = SimulatedDSLogic()
    results = self_test.run_self_test(device)
    assert all(result.severity == "ok" for result in results), results
    items = by_item(results)
    assert {"FIRMWARE", "FPGA", "USB", "SECURITY", "BUFFER_CAPTURE", "STREAM_CAPTURE",
            "PATTERN_TRIGGER", "EDGE_TRIGGER", "CH1", "CH16"} <= set(items)
    assert "4096 Mbit" in items["BUFFER_CAPTURE"].detail
    assert "20 MHz" in items["STREAM_CAPTURE"].detail
    assert "sample 165" in items["PATTERN_TRIGGER"].detail  # 0xA5
    assert "sample 16" in items["EDGE_TRIGGER"].detail  # first rising edge of channel 5
    # the counter checks use the test mode, the input check the inputs
    assert [internal for _session, internal in device.sessions] == [True] * 4 + [False]


def test_a_broken_data_line_names_the_channel():
    device = SimulatedDSLogic()
    device.broken_bit = 6
    items = by_item(self_test.run_self_test(device))
    assert items["BUFFER_CAPTURE"].severity == "fail"
    assert "channels 7" in items["BUFFER_CAPTURE"].detail
    assert items["STREAM_CAPTURE"].severity == "fail"


def test_connected_or_stuck_inputs_are_warnings():
    device = SimulatedDSLogic()
    total = self_test.INPUT_SAMPLES
    device.inputs = {2: np.ones(total, dtype=np.uint8), 5: np.arange(total, dtype=np.uint8) & 1}
    items = by_item(self_test.run_self_test(device))
    assert items["CH3"].status == "STUCK_HIGH" and items["CH3"].severity == "warning"
    assert items["CH6"].status == "ACTIVE" and "edges" in items["CH6"].detail
    assert items["CH1"].severity == "ok"


def test_failed_security_and_usb2_on_a_usb3_board():
    device = SimulatedDSLogic(security_passed=False)
    assert by_item(self_test.run_self_test(device))["SECURITY"].severity == "fail"
    u3 = by_item(self_test.run_self_test(SimulatedDSLogic(pid=0x002A)))
    assert u3["USB"].status == "SLOW" and "SECURITY" not in u3


def test_a_capture_error_fails_only_that_check():
    device = SimulatedDSLogic()
    device.start_capture = lambda session, completed, internal_test=False: CaptureError.HARDWARE_ERROR
    items = by_item(self_test.run_self_test(device))
    assert items["BUFFER_CAPTURE"].severity == "fail" and "hardware_error" in items["BUFFER_CAPTURE"].detail
    assert items["INPUTS"].severity == "fail"
    assert items["FIRMWARE"].severity == "ok"


def test_titles_keep_acronyms():
    assert SelfTestResult("FPGA", "OK").title == "FPGA"
    assert SelfTestResult("BUFFER_CAPTURE", "OK").title == "Buffer capture"
    assert SelfTestResult("RAM", "OK").title == "RAM"
    assert SelfTestResult("TRIGGER_LINK", "OK").title == "Trigger link"
