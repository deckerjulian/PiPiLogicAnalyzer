# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer 7, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Driver that aggregates 2 to 5 cascaded devices.

Port of ``SharedDriver/MultiAnalyzerDriver.cs``.  One board evaluates the trigger and drives its
trigger output; the other boards capture on an edge of their external trigger input, which is
wired to that output.  The external trigger follows the last capture channel of each device
(channel 24 on 24 channel boards, but e.g. 28 on the 28 channel boards supported since V6_5).

The original always let the first board (the master) evaluate a pattern.  The trigger output and
the trigger input of every board form one line, so here the board of the trigger channel does it:

* pattern trigger: on any board, inside one group of consecutive trigger inputs of that board;
* edge trigger on a channel: that board drives the trigger output on the edge, which needs the
  firmware of this project (capability ``EDGE_TRIGGER_OUT``);
* edge trigger on the external trigger input (``trigger_channel == channel_count``): every board
  waits for the edge on its own trigger input, so all of them start without a delay to compensate.
"""

from __future__ import annotations

import threading
from typing import Optional, Sequence, Union

from .analyzer import PiPiLogicAnalyzerDriver
from .base import (
    COMPLEX_TRIGGER_DELAY,
    EDGE_OUT_TRIGGER_DELAY,
    FAST_TRIGGER_DELAY,
    AnalyzerDriverBase,
    AnalyzerDriverType,
    CaptureCompletedArgs,
    CaptureCompletedHandler,
    CaptureError,
    CaptureLimits,
    CAPABILITY_EDGE_TRIGGER_OUT,
    CAPABILITY_SELF_TEST,
    CaptureMode,
    DeviceConnectionError,
    SelfTestResult,
    UnsupportedFeatureError,
    parse_version,
    pattern_fits,
    pattern_max_bits,
)
from .models import AnalyzerChannel, CaptureSession, TriggerType


class MultiAnalyzerDriver(AnalyzerDriverBase):
    def __init__(self, connection_strings: Sequence[str]) -> None:
        super().__init__()
        if connection_strings is None or not (2 <= len(connection_strings) <= 5):
            raise ValueError("Invalid devices specified, 2 to 5 connection strings must be provided")

        self._lock = threading.RLock()
        self._capturing = False
        self._devices: list[PiPiLogicAnalyzerDriver] = []
        self._pending: list[Optional[CaptureSession]] = []
        self._completed: list[bool] = []
        self._source_session: Optional[CaptureSession] = None
        self._current_handler: Optional[CaptureCompletedHandler] = None

        try:
            for connection_string in connection_strings:
                self._devices.append(PiPiLogicAnalyzerDriver(connection_string))
        except Exception as error:
            for device in self._devices:
                device.dispose()
            raise DeviceConnectionError(f"Error connecting to the devices: {error}") from error

        master_version = None
        for index, device in enumerate(self._devices):
            version = parse_version(device.device_version)
            if not version.is_valid:
                self.dispose()
                raise DeviceConnectionError(
                    f"Invalid device version ({device.device_version}) found on device "
                    f"{connection_strings[index]}"
                )
            if master_version is None:
                master_version = version
            elif (master_version.major, master_version.minor) != (version.major, version.minor):
                self.dispose()
                raise DeviceConnectionError(
                    f"Different device versions found. Master version: "
                    f"V{master_version.major}_{master_version.minor}, device "
                    f"{connection_strings[index]} version: V{version.major}_{version.minor}."
                )

        assert master_version is not None
        self._version = f"MULTI_ANALYZER_{master_version.major}_{master_version.minor}"

        for index, device in enumerate(self._devices):
            device.tag = index

    # ------------------------------------------------------------- properties
    @property
    def devices(self) -> list[PiPiLogicAnalyzerDriver]:
        return list(self._devices)

    @property
    def device_version(self) -> Optional[str]:
        return self._version

    @property
    def channels_per_device(self) -> int:
        return min(device.channel_count for device in self._devices)

    @property
    def channel_count(self) -> int:
        return self.channels_per_device * len(self._devices)

    @property
    def max_frequency(self) -> int:
        return min(device.max_frequency for device in self._devices)

    @property
    def min_frequency(self) -> int:
        return max(device.min_frequency for device in self._devices)

    @property
    def buffer_size(self) -> int:
        return min(device.buffer_size for device in self._devices)

    @property
    def blast_frequency(self) -> int:
        return 0

    @property
    def driver_type(self) -> AnalyzerDriverType:
        return AnalyzerDriverType.MULTI

    @property
    def is_network(self) -> bool:
        return False

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    # ---------------------------------------------------------------- helpers
    def _split_channels_per_device(self, channels: Sequence[int]) -> list[list[int]]:
        per_device = self.channels_per_device
        return [
            [c - device * per_device
             for c in channels
             if device * per_device <= c < (device + 1) * per_device]
            for device in range(len(self._devices))
        ]

    def get_capture_mode(self, channels: Sequence[int]) -> CaptureMode:
        split = self._split_channels_per_device(channels)
        max_channel = max((max(group) for group in split if group), default=0)
        if max_channel < 8:
            return CaptureMode.CHANNELS_8
        if max_channel < 16:
            return CaptureMode.CHANNELS_16
        return CaptureMode.CHANNELS_24

    def get_limits(self, channels: Sequence[int]) -> CaptureLimits:
        split = self._split_channels_per_device(channels)
        limits = [device.get_limits(group) for device, group in zip(self._devices, split)]
        return CaptureLimits(
            min_pre_samples=max(limit.min_pre_samples for limit in limits),
            max_pre_samples=min(limit.max_pre_samples for limit in limits),
            min_post_samples=max(limit.min_post_samples for limit in limits),
            max_post_samples=min(limit.max_post_samples for limit in limits),
        )

    # --------------------------------------------------------------- triggers
    def pattern_trigger_groups(self) -> tuple[tuple[int, int], ...]:
        """The groups of every board as channels of the set; a pattern cannot span two boards."""
        per_device = self.channels_per_device
        groups = []
        for index, device in enumerate(self._devices):
            for first, count in device.pattern_trigger_groups():
                count = min(first + count, per_device) - first
                if count > 0:
                    groups.append((index * per_device + first, count))
        return tuple(groups)

    def edge_trigger_channels(self) -> list[int]:
        """Channels of the boards whose firmware drives the trigger output on an edge."""
        per_device = self.channels_per_device
        return [
            index * per_device + channel
            for index, device in enumerate(self._devices)
            if CAPABILITY_EDGE_TRIGGER_OUT in device.capabilities()
            for channel in range(per_device)
        ]

    def _trigger_plan(
        self, session: CaptureSession
    ) -> Union[CaptureError, tuple[Optional[int], TriggerType, float]]:
        """(board evaluating the trigger, ``None`` for the external trigger; its trigger type; delay)."""
        if session.trigger_type == TriggerType.EDGE and session.trigger_channel == self.channel_count:
            return None, TriggerType.EDGE, 0.0
        if not 0 <= session.trigger_channel < self.channel_count:
            return CaptureError.BAD_PARAMS

        device = session.trigger_channel // self.channels_per_device
        if session.trigger_type == TriggerType.EDGE:
            if CAPABILITY_EDGE_TRIGGER_OUT not in self._devices[device].capabilities():
                return CaptureError.UNSUPPORTED
            return device, TriggerType.EDGE_OUT, EDGE_OUT_TRIGGER_DELAY

        if not (
            1 <= session.trigger_bit_count <= pattern_max_bits(session.trigger_type)
            and pattern_fits(
                self.pattern_trigger_groups(), session.trigger_channel, session.trigger_bit_count
            )
        ):
            return CaptureError.BAD_PARAMS
        delay = FAST_TRIGGER_DELAY if session.trigger_type == TriggerType.FAST else COMPLEX_TRIGGER_DELAY
        return device, session.trigger_type, delay

    # ------------------------------------------------------------ board test
    def capabilities(self) -> frozenset[str]:
        # Simulation needs a single device; the self-test runs on every device.
        if all(CAPABILITY_SELF_TEST in device.capabilities() for device in self._devices):
            return frozenset({CAPABILITY_SELF_TEST})
        return frozenset()

    def run_self_test(self) -> list[SelfTestResult]:
        if CAPABILITY_SELF_TEST not in self.capabilities():
            raise UnsupportedFeatureError("Not every device of the set has a self-test firmware.")
        results: list[SelfTestResult] = []
        for number, device in enumerate(self._devices, start=1):
            for result in device.run_self_test():
                result.item = f"DEVICE{number}_{result.item}"
                results.append(result)
        return results

    # ---------------------------------------------------------------- capture
    def start_capture(
        self, session: CaptureSession, completed_handler: Optional[CaptureCompletedHandler] = None
    ) -> CaptureError:
        if session.trigger_type == TriggerType.SIMULATION:
            return CaptureError.UNSUPPORTED

        if session.trigger_type not in (TriggerType.EDGE, TriggerType.COMPLEX, TriggerType.FAST):
            # Blast captures have no pre-trigger samples to compensate the trigger delay with.
            return CaptureError.BAD_PARAMS

        with self._lock:
            if self._capturing:
                return CaptureError.BUSY
            if not session.capture_channels:
                return CaptureError.BAD_PARAMS

            channels = session.channel_numbers
            limits = self.get_limits(channels)

            if not (
                min(channels) >= 0
                and max(channels) <= self.channel_count - 1
                and session.loop_count == 0
                and limits.min_pre_samples <= session.pre_trigger_samples <= limits.max_pre_samples
                and limits.min_post_samples <= session.post_trigger_samples <= limits.max_post_samples
                and session.pre_trigger_samples + session.post_trigger_samples
                <= limits.max_total_samples
                and self.min_frequency <= session.frequency <= self.max_frequency
            ):
                return CaptureError.BAD_PARAMS

            plan = self._trigger_plan(session)
            if isinstance(plan, CaptureError):
                return plan
            trigger_device, trigger_type, delay = plan

            channels_per_device = self._split_channels_per_device(channels)
            if trigger_device is not None and not channels_per_device[trigger_device]:
                # The board evaluating the trigger has to run a capture of its own.
                return CaptureError.BAD_PARAMS

            offset = 0
            if trigger_device is not None:
                sample_period = 1_000_000_000.0 / session.frequency
                offset = int(round((delay / sample_period) + 0.3))

            self._completed = [False] * len(self._devices)
            self._pending = [None] * len(self._devices)
            self._current_handler = completed_handler
            self._source_session = session
            self._capturing = True

            # Boards started through their trigger input first: they must be armed before the
            # trigger fires.
            for index, device_channels in enumerate(channels_per_device):
                if index == trigger_device:
                    continue
                if not device_channels:
                    self._completed[index] = True
                    continue

                device_session = session.clone_settings()
                device_session.capture_channels = [
                    AnalyzerChannel(channel_number=number) for number in device_channels
                ]
                device_session.trigger_channel = self._devices[index].channel_count
                device_session.trigger_type = TriggerType.EDGE
                device_session.pre_trigger_samples = session.pre_trigger_samples + offset
                device_session.post_trigger_samples = session.post_trigger_samples - offset
                device_session.loop_count = 0
                device_session.measure_bursts = False
                # The trigger output rises; an external trigger keeps the selected edge.
                device_session.trigger_inverted = session.trigger_inverted if trigger_device is None else False

                error = self._devices[index].start_capture(
                    device_session, self._make_device_handler(index)
                )
                if error != CaptureError.NONE:
                    self.stop_capture()
                    return error

            if trigger_device is not None:
                trigger_session = session.clone_settings()
                trigger_session.capture_channels = [
                    AnalyzerChannel(channel_number=number)
                    for number in channels_per_device[trigger_device]
                ]
                trigger_session.trigger_type = trigger_type
                trigger_session.trigger_channel = (
                    session.trigger_channel - trigger_device * self.channels_per_device
                )
                trigger_session.loop_count = 0
                trigger_session.measure_bursts = False

                error = self._devices[trigger_device].start_capture(
                    trigger_session, self._make_device_handler(trigger_device)
                )
                if error != CaptureError.NONE:
                    self.stop_capture()
                    return error

            return CaptureError.NONE

    def _make_device_handler(self, index: int) -> CaptureCompletedHandler:
        """Bind a completion callback to the device that produced it.

        The original code looked the index up through a mutable ``Tag`` property
        on the driver, which raced when several devices finished at once.
        """

        def handler(args: CaptureCompletedArgs) -> None:
            self._device_capture_completed(index, args)

        return handler

    def _device_capture_completed(self, index: int, args: CaptureCompletedArgs) -> None:
        with self._lock:
            if not self._capturing or self._source_session is None:
                return

            if not args.success:
                self.stop_capture()
                session = self._source_session
                handler = self._current_handler
                self._pending = []
                self._raise_capture_completed(
                    CaptureCompletedArgs(success=False, session=session, error=args.error), handler
                )
                return

            self._pending[index] = args.session
            self._completed[index] = True

            if not all(self._completed):
                return

            per_device = self.channels_per_device
            destination = {c.channel_number: c for c in self._source_session.capture_channels}
            for device_index, device_session in enumerate(self._pending):
                if device_session is None:
                    continue
                for channel in device_session.capture_channels:
                    target = destination.get(channel.channel_number + device_index * per_device)
                    if target is not None:
                        target.samples = channel.samples

            self._capturing = False
            self._raise_capture_completed(
                CaptureCompletedArgs(success=True, session=self._source_session),
                self._current_handler,
            )

    def stop_capture(self) -> bool:
        if not self._capturing:
            return False
        for device in self._devices:
            device.stop_capture()
        self._capturing = False
        return True

    def enter_bootloader(self) -> bool:
        if self._capturing:
            return False
        return all(device.enter_bootloader() for device in self._devices)

    def dispose(self) -> None:
        for device in self._devices:
            device.dispose()
        super().dispose()
