"""Decoders name their capture channels by channel number: reordering the channels, a capture
with other channels or a profile with a selection of channels never makes them read another
channel."""

from __future__ import annotations

import threading

import pytest

from openscilab import api
from openscilab.core.profiles import Profile, map_decoder_channels
from openscilab.driver.models import AnalyzerChannel, CaptureSession, TriggerType
from openscilab.driver.simulated import open_simulated
from openscilab.sigrok.composition import compose
from openscilab.sigrok.engine import DecoderRegistry
from openscilab.sigrok.provider import DecoderInstance, SigrokProvider


def uart_session(numbers=(0, 1, 2, 3), samples: int = 40_000) -> CaptureSession:
    """UART text on channel number 2 (the third channel of the free simulator)."""
    instrument = open_simulated("free", fast=True, clock=lambda: 0.0,
                                signals={"scenario": "uart", "text": "Hi\\n", "baud": 9600, "channel": 3})
    session = CaptureSession(frequency=100_000, pre_trigger_samples=0, post_trigger_samples=samples,
                             trigger_type=TriggerType.IMMEDIATE)
    session.capture_channels = [AnalyzerChannel(channel_number=number) for number in numbers]
    done = threading.Event()
    assert instrument.capture.driver.start_capture(session, lambda args: done.set()).name == "NONE"
    assert done.wait(10)
    return session


@pytest.fixture(scope="module")
def registry():
    registry = DecoderRegistry()
    registry.load()
    return registry


def texts(groups) -> list[str]:
    return [segment.values[0] for group in groups for row in group.annotations
            if "data" in row.name for segment in row.segments]


def uart(registry, number: int) -> SigrokProvider:
    info = registry.get("uart")
    rx = next(channel.index for channel in info.channels if channel.id == "rx")
    provider = SigrokProvider(registry)
    provider.add_instance(DecoderInstance("uart", channel_map={rx: number},
                                          options={**info.default_options(), "baudrate": 9600, "format": "ascii"}))
    return provider


def test_reordered_channels_decode_the_same(registry, make_dataview):
    session = uart_session()
    provider = uart(registry, 2)
    before = texts(provider.run(session))
    assert before and any(text == "H" for text in before)

    view = make_dataview(provider=provider)
    view.load_session(session)
    assert view.model.move_channel(view.model.channels[2], 0)
    assert [channel.channel_number for channel in view.model.channels] == [2, 0, 1, 3]
    assert texts(provider.run(view.model.session)) == before
    view.undo.undo()
    assert texts(provider.run(view.model.session)) == before


def test_the_channel_is_found_by_its_number(registry):
    session = uart_session(numbers=(3, 2))  # channel number 2 is at position 1
    assert any(text == "H" for text in texts(uart(registry, 2).run(session)))
    assert not any(text == "H" for text in texts(uart(registry, 3).run(session)))


def test_a_channel_the_capture_does_not_have_is_reported(registry):
    session = uart_session(numbers=(0, 1))
    provider = uart(registry, 2)
    instance = provider.instances[0]
    assert provider.missing_channels(instance) == []
    assert provider.unavailable_channels(instance, session.capture_channels) == ["RX"]
    assert texts(provider.run(session)) == []


def test_the_api_assigns_by_number(registry):
    capture = api.Capture(uart_session(numbers=(3, 2)))
    records = capture.decode("uart", {"rx": 2}, {"baudrate": 9600, "format": "ascii"}, registry=registry)
    assert any(record.value == "H" for record in records)


def test_the_composition_reads_the_assigned_channel(registry):
    session = uart_session(numbers=(3, 2))
    provider = uart(registry, 2)
    group = provider.run(session)[0]
    segment = group.annotations[0].segments[0]
    composition = compose(group.info, group.instance, session.capture_channels, segment)
    assert composition is not None and [bit.capture_number for bit in composition.bits] == [2]


def test_profiles_store_positions_and_work_with_numbers():
    settings = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=100)
    settings.capture_channels = [AnalyzerChannel(channel_number=number) for number in (0, 3, 5)]
    stored = {"Name": "p", "CaptureSettings": Profile("p", settings).to_dict()["CaptureSettings"],
              "DecoderConfiguration": [{"decoder_id": "uart", "channel_map": {"0": 1}, "parent": None}]}
    profile = Profile.from_dict(stored)
    assert profile.decoder_configuration[0]["channel_map"] == {"0": 3}  # position 1 is channel number 3
    assert profile.to_dict()["DecoderConfiguration"][0]["channel_map"] == {"0": 1}
    assert stored["DecoderConfiguration"][0]["channel_map"] == {"0": 1}  # the input is not changed

    # a channel the profile does not capture is not assigned in the file
    profile.decoder_configuration[0]["channel_map"] = {"0": 4}
    assert profile.to_dict()["DecoderConfiguration"][0]["channel_map"] == {}


def test_the_tree_of_the_original_software_is_translated():
    tree = {"Branches": [{"DecoderId": "uart", "Channels": [{"SigrokIndex": 0, "CaptureIndex": 1},
                                                             {"SigrokIndex": 1, "CaptureIndex": -1}],
                          "Children": [{"DecoderId": "x", "Channels": [{"SigrokIndex": 0, "CaptureIndex": 2}]}]}]}
    numbers = [0, 3, 5]
    mapped = map_decoder_channels(tree, lambda position: numbers[position] if position < len(numbers) else None)
    branch = mapped["Branches"][0]
    assert [channel["CaptureIndex"] for channel in branch["Channels"]] == [3, -1]
    assert branch["Children"][0]["Channels"][0]["CaptureIndex"] == 5
    assert tree["Branches"][0]["Channels"][0]["CaptureIndex"] == 1


# ------------------------------------------------------ the decoder thread
def wait_for(condition, timeout: float = 10.0) -> bool:
    import time

    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_closing_a_view_ends_its_decoders(registry, make_dataview, shell):
    from openscilab.ui.widgets import decoder_manager

    provider = uart(registry, 2)
    view = make_dataview(provider=provider)
    view.load_session(uart_session(samples=2_000_000))  # long enough to still decode when it closes
    assert view.decoder_manager.decoding
    worker = view.decoder_manager._worker
    shell.area.close_document(view)  # must not destroy a running thread
    assert wait_for(lambda: worker not in decoder_manager._running_workers)
    assert view.decoder_manager._worker is None


def test_an_edit_decodes_once(registry, make_dataview, monkeypatch):
    provider = uart(registry, 2)
    view = make_dataview(provider=provider)
    view.load_session(uart_session())
    assert wait_for(lambda: view.model.annotation_groups and not view.decoder_manager.decoding)
    from openscilab.sigrok import worker

    runs = []
    run = worker.run
    monkeypatch.setattr(worker, "run", lambda *args, **kwargs: (runs.append(1), run(*args, **kwargs))[1])
    view.delete_samples(0, 10)
    assert wait_for(lambda: runs and not view.decoder_manager.decoding)
    assert wait_for(lambda: not view.decoder_manager.decoding) and len(runs) == 1
    view.undo.undo()
    assert wait_for(lambda: len(runs) >= 2 and not view.decoder_manager.decoding) and len(runs) == 2


def test_results_name_the_decoders_of_the_view(registry):
    provider = uart(registry, 2)
    frozen = provider.frozen()
    provider.instances[0].options["baudrate"] = 1200  # changed while the copy decodes
    groups = frozen.run(uart_session())
    assert groups[0].instance is provider.instances[0]
    assert any(text == "H" for text in texts(groups))  # decoded with the settings it was started with


def test_a_cancelled_decode_ends_early(registry):
    provider = uart(registry, 2)
    calls = []

    def cancelled() -> bool:
        calls.append(1)
        return len(calls) > 50

    groups = provider.run(uart_session(), cancelled=cancelled)
    assert len(calls) < 60  # it stopped at the next step
    assert len(texts(groups)) < 10


# ------------------------------------------------- found by the second check
def test_a_profile_keeps_a_decoder_channel_it_does_not_capture():
    settings = CaptureSession(frequency=1_000_000, pre_trigger_samples=0, post_trigger_samples=100)
    settings.capture_channels = [AnalyzerChannel(channel_number=number) for number in (0, 1, 2)]
    profile = Profile("p", settings, [{"decoder_id": "uart", "channel_map": {"0": 5, "1": 2}, "parent": None}])
    stored = profile.to_dict()
    item = stored["DecoderConfiguration"][0]
    assert item["channel_map"] == {"1": 2} and item["channel_numbers"] == {"0": 5}  # positions, and the rest
    again = Profile.from_dict(stored)
    assert again.decoder_configuration[0]["channel_map"] == {"1": 2, "0": 5}  # the same after a restart
    assert "channel_numbers" not in again.decoder_configuration[0]
    assert Profile.from_dict(again.to_dict()).decoder_configuration == again.decoder_configuration


def test_the_options_keep_a_channel_the_capture_does_not_have(registry, qtbot):
    from openscilab.ui.dialogs.decoder_options import DecoderOptionsDialog

    session = uart_session(numbers=(0, 1))
    provider = uart(registry, 7)  # a profile made for a capture with more channels
    instance = provider.instances[0]
    info = registry.get("uart")
    dialog = DecoderOptionsDialog(info, instance, session.capture_channels)
    qtbot.addWidget(dialog)
    rx = next(channel.index for channel in info.channels if channel.id == "rx")
    assert dialog._channel_widgets[rx].currentData() == 7
    assert "not in this capture" in dialog._channel_widgets[rx].currentText()
    dialog._accept()  # e.g. after changing an option
    assert instance.channel_map[rx] == 7  # not dropped
