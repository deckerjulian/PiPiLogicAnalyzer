"""The parsers against arbitrary input (hypothesis): what reads a file, a frame of a protocol, a
line of a device or an address either returns or raises the error it documents - never a
TypeError, KeyError or IndexError from inside, never a hang. Round trips: what is written is read
back the same."""

from __future__ import annotations

import json
import os
import zipfile

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from openscilab.core import capture_io, units
from openscilab.core.sigrok_session import SigrokSessionError, load_session
from openscilab.core.units import UnitError
from openscilab.core.waveform import Waveform, WaveformError
from openscilab.driver.arduino import protocol as arduino
from openscilab.driver.pico import protocol as pico
from openscilab.driver.pico.instrument import parse_pins
from openscilab.lab import panel_model, yaml_io
from openscilab.lab.model import Edge, Flow, FlowError, PortRef
from openscilab.sdl.parser import SDLError, get_tokens, samples_from_source
from openscilab_device.protocol import ProtocolError, check_description

QUICK = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

text = st.text(max_size=400)
#: YAML and JSON documents of any shape, as a text
json_like = st.recursive(
    st.none() | st.booleans() | st.integers(-10**9, 10**9) | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=12), children, max_size=5),
    max_leaves=25)


def as_yaml(data) -> str:
    import yaml

    return yaml.safe_dump(data, allow_unicode=True)


# ------------------------------------------------------------------- files
@QUICK
@given(text)
def test_a_flow_file_is_a_flow_or_a_flow_error(source):
    try:
        yaml_io.loads(source)
    except FlowError:
        pass


@QUICK
@given(json_like)
def test_a_flow_document_of_any_shape_is_a_flow_or_a_flow_error(data):
    try:
        flow = yaml_io.loads(as_yaml(data))
    except FlowError:
        return
    assert isinstance(flow, Flow)
    flow.validate()  # (problems, not exceptions)


@QUICK
@given(text)
def test_a_panel_file_is_a_panel_or_a_panel_error(source):
    try:
        panel_model.loads(source)
    except panel_model.PanelError:
        pass


@QUICK
@given(json_like)
def test_a_panel_document_of_any_shape_is_a_panel_or_a_panel_error(data):
    try:
        panel = panel_model.loads(as_yaml(data))
    except panel_model.PanelError:
        return
    assert panel_model.loads(panel_model.dumps(panel)).to_data() == panel.to_data()


@QUICK
@given(json_like)
def test_a_capture_of_any_shape_is_a_capture_or_a_value_error(data):
    try:
        capture_io.capture_from_dict(data if isinstance(data, dict) else {"Settings": data})
    except ValueError:
        pass


@QUICK
@given(st.binary(max_size=2000))
def test_a_capture_file_of_any_bytes_is_refused_cleanly(tmp_path_factory, data):
    path = tmp_path_factory.mktemp("captures") / "any.lac"
    path.write_bytes(data)
    try:
        capture_io.load_capture(str(path))
    except (ValueError, OSError):
        pass
    try:
        load_session(str(path))
    except (SigrokSessionError, OSError):
        pass


@QUICK
@given(st.dictionaries(st.sampled_from(["version", "metadata", "logic-1-1", "other"]), st.binary(max_size=200),
                       max_size=4))
def test_a_sigrok_session_of_any_members_is_refused_cleanly(tmp_path_factory, members):
    path = tmp_path_factory.mktemp("sessions") / "any.sr"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    try:
        load_session(str(path))
    except (SigrokSessionError, OSError):
        pass


@QUICK
@given(json_like)
def test_a_waveform_of_any_shape_is_a_waveform_or_a_waveform_error(data):
    try:
        Waveform.from_data(data if isinstance(data, dict) else {"kind": data})
    except WaveformError:
        pass


# ----------------------------------------------------------------- devices
@QUICK
@given(text)
def test_the_lines_of_a_pico_never_break_their_parsers(line):
    pico.parse_version(line)
    pico.parse_self_test_line(line)
    parse_pins(line.splitlines())


@QUICK
@given(st.frozensets(st.text(max_size=30), max_size=8))
def test_the_capabilities_of_a_pico_never_break_their_parsers(capabilities):
    pico.parse_trigger_sequence(capabilities)
    pico.parse_state_max_clock(capabilities)
    pico.parse_pattern_groups(capabilities)


@QUICK
@given(st.binary(max_size=600))
def test_the_bytes_of_an_arduino_are_frames_or_counted_as_damaged(data):
    decoder = arduino.Decoder()
    frames = decoder.feed(data)
    assert all(isinstance(frame, arduino.Frame) for frame in frames)
    try:
        arduino.decode(data)
    except arduino.ProtocolError:
        pass
    try:
        arduino.cobs_decode(data)
    except arduino.ProtocolError:
        pass
    arduino.parse_text(data)
    arduino.unpack_runs(data)


@QUICK
@given(st.integers(0, 255), st.integers(0, 255), st.binary(max_size=300))
def test_an_arduino_frame_comes_back_as_it_was_sent(frame_type, sequence, payload):
    frames = arduino.Decoder().feed(arduino.encode(frame_type, sequence, payload))
    assert len(frames) == 1
    assert (frames[0].type, frames[0].sequence, frames[0].payload) == (frame_type, sequence, payload)


@QUICK
@given(json_like)
def test_the_description_of_a_remote_device_is_checked_or_refused(description):
    try:
        check_description(description)
    except ProtocolError:
        pass


# ------------------------------------------------------------ small parsers
@QUICK
@given(text)
def test_a_quantity_is_a_number_or_a_unit_error(source):
    try:
        units.parse(source)
    except UnitError:
        pass


@QUICK
@given(st.text(max_size=120))
def test_a_signal_description_is_samples_or_an_sdl_error(source):
    try:
        get_tokens(source)
        samples_from_source(source, 64)
    except SDLError:
        pass


@QUICK
@given(text)
def test_a_port_or_an_edge_is_parsed_or_a_flow_error(source):
    try:
        PortRef.parse(source)
    except FlowError:
        pass
    try:
        Edge.parse(source)
    except FlowError:
        pass


# -------------------------------------------------------------- round trips
names = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)


@QUICK
@given(st.dictionaries(names, st.sampled_from(["control.timer", "dsp.math", "view.number", "structure.comment"]),
                       min_size=1, max_size=5), st.text(max_size=30))
def test_a_flow_is_written_and_read_back_the_same(nodes, description):
    flow = Flow(name="Round trip", description=description)
    for node_id, node_type in nodes.items():
        flow.add_node(node_type, node_id)
    again = yaml_io.loads(yaml_io.dumps(flow))
    assert again.description == description and set(again.nodes) == set(nodes)
    assert {node_id: node.type for node_id, node in again.nodes.items()} == nodes


@QUICK
@given(st.lists(st.tuples(st.sampled_from(list(panel_model.WIDGET_KINDS)), st.integers(0, 2000), st.integers(0, 2000),
                          st.integers(panel_model.MIN_SIZE[0], 800), st.integers(panel_model.MIN_SIZE[1], 600),
                          st.text(max_size=12)), max_size=6))
def test_a_panel_is_written_and_read_back_the_same(widgets):
    panel = panel_model.Panel(name="Round trip")
    for kind, x, y, width, height, title in widgets:
        panel.add(kind, x=x, y=y, width=width, height=height, title=title)
    again = panel_model.loads(panel_model.dumps(panel))
    assert again.to_data() == panel.to_data()
    assert [widget.rect for widget in again.widgets] == [widget.rect for widget in panel.widgets]


def test_the_capture_json_round_trip_keeps_the_settings(tmp_path):
    import numpy as np

    from openscilab.driver.models import AnalyzerChannel, CaptureSession

    session = CaptureSession(frequency=12_345, pre_trigger_samples=3, post_trigger_samples=29)
    session.capture_channels = [AnalyzerChannel(channel_number=1, channel_name="SCL", samples=np.arange(32) % 2)]
    path = str(tmp_path / "round.lac")
    capture_io.save_capture(path, session)
    again = capture_io.load_capture(path).session
    assert again.frequency == 12_345 and again.capture_channels[0].channel_name == "SCL"
    assert np.array_equal(again.capture_channels[0].samples, session.capture_channels[0].samples)
    assert os.path.exists(path) and json.load(open(path, encoding="utf-8"))["Settings"]


# ------------------------------------------------- what the fuzzing found once
@pytest.mark.parametrize("text", ["line one\nline\x08two", "\x85", "a b", "tab\tand\r\nreturn", "﻿bom"])
def test_text_with_control_characters_reads_back_as_it_was(text):
    flow = Flow(name="Round trip", description=text)
    flow.add_node("structure.comment", "note", text=text)
    again = yaml_io.loads(yaml_io.dumps(flow))  # (a block with \x08 made the file unreadable)
    assert again.description == text and again.nodes["note"].params["text"] == text
    panel = panel_model.Panel(name=text)
    panel.add("label", title=text, options={"text": text})
    assert panel_model.loads(panel_model.dumps(panel)).to_data() == panel.to_data()  # (\x85 came back as " ")


def test_a_session_file_with_damaged_metadata_says_so(tmp_path):
    path = tmp_path / "damaged.sr"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version", b"1")
        archive.writestr("metadata", b"\x00")
    with pytest.raises(SigrokSessionError, match="metadata cannot be read"):
        load_session(str(path))
