"""The data of a run: collected per output (lab/run_data.py), shown in the nodes of the graph and in
data views of a node or of the whole run."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from openscilab.core import signals
from openscilab.lab import yaml_io
from openscilab.lab.run_data import RunData

FLOW = """flow: Clock
nodes:
  sim: {type: device.instrument, address: sim:free}
  capture: {type: device.capture, channels: [D15, D8], rate: 1 MHz, samples: 4000}
  frequency: {type: measure.frequency}
  check: {type: report.check, name: Clock, expected: 1000, tolerance: 1, unit: Hz}
  points: {type: data.table, columns: [f]}
  scope: {type: view.scope}
edges:
  - sim.device -> capture.device
  - capture.D15 -> frequency.in
  - frequency.out -> check.in
  - frequency.out -> points.f
  - capture.capture -> scope.in
"""


def wait_for(condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# --------------------------------------------------------------------- model
def test_captures_and_numbers_on_one_time_axis():
    data = RunData()
    first = signals.Capture(rate=1000.0, start=1.0, digital={"D0": np.array([1, 0, 1, 0])},
                            analog={"CH1": np.array([0.5, 1.0, 1.5, 2.0])})
    second = signals.Capture(rate=1000.0, start=2.0, digital={"D0": np.array([1, 1, 1, 1])},
                             analog={"CH1": np.array([3.0, 3.0, 3.0, 3.0])})
    data.add("cap", "capture", [first, second], [1.0, 2.0])
    data.add("mean", "out", [signals.Scalar(value=1.25, unit="V", at=1.004), signals.Scalar(value=3.0, unit="V", at=2.004)])
    data.add("check", "pass", [signals.Bool(value=True, at=2.5)])
    data.add("cap", "done", [signals.Event(times=[1.0])])
    assert data.get("cap", "capture").kind == "signal" and data.get("mean", "out").kind == "number"
    assert data.get("cap", "done").kind == "event" and not data.get("cap", "done").viewable

    session = data.to_session()
    assert session.frequency == 1000
    digital = {channel.channel_name: channel.samples for channel in session.capture_channels}
    analog = {channel.channel_name: channel.volts() for channel in session.analog_channels}
    assert set(digital) == {"cap.D0", "check.pass"} and set(analog) == {"cap.CH1", "mean.out"}
    assert list(digital["cap.D0"][:4]) == [1, 0, 1, 0]  # the first capture at the start of the axis
    assert list(digital["cap.D0"][1000:1004]) == [1, 1, 1, 1]  # the second one second later
    assert np.allclose(analog["cap.CH1"][:4], [0.5, 1.0, 1.5, 2.0], atol=1e-3)
    assert np.isclose(analog["mean.out"][500], 1.25, atol=1e-3) and np.isclose(analog["mean.out"][1100], 3.0, atol=1e-3)
    assert digital["check.pass"][-1] == 1 and digital["check.pass"][0] == 0
    assert [channel.channel_name for channel in data.to_session(["mean.out"]).analog_channels] == ["mean.out"]
    assert data.to_session(["cap.done"]) is None


def test_a_long_run_is_sampled_down_to_fit():
    data = RunData()
    data.add("cap", "capture", [signals.Capture(rate=1e6, start=0.0, digital={"D0": np.zeros(10)}),
                                signals.Capture(rate=1e6, start=100.0, digital={"D0": np.ones(10)})])
    session = data.to_session(max_samples=10_000)
    assert session.post_trigger_samples <= 10_002 and session.capture_channels[0].samples[-1] == 1


# ---------------------------------------------------------------------- graph
def run_flow(shell):
    document = shell.new_flow()
    flow = yaml_io.loads(FLOW)
    document._edit("Load", lambda target: target.__dict__.update(flow.copy().__dict__))
    document.fast_box.setChecked(True)
    assert document.start_run()
    assert wait_for(lambda: document.runner.result is not None)
    assert document.runner.result.state == "finished", document.runner.result.error
    return document


def test_the_nodes_show_their_data_and_open_it(shell):
    from openscilab.ui.documents.chart import ValueDocument
    from openscilab.ui.documents.dataview import DataView

    document = run_flow(shell)
    scene = document.scene
    capture = scene.nodes["capture"]
    assert capture.data_entries and capture.data_entries[0].kind == "signal"
    assert capture.data_height() > 0 and capture.boundingRect().height() > capture._base_size()[1]
    assert scene.nodes["frequency"].data_entries[0].key == "frequency.out"
    assert scene.nodes["scope"].data_entries[0].key == "capture.capture"  # a view shows what arrives
    assert not scene.nodes["sim"].data_entries  # a device has no data to show
    assert not document.grab().isNull()

    scene.node_double_clicked("capture", on_data=True)  # double-click on the data: in a data view
    view = shell.active_document()
    assert isinstance(view, DataView) and "capture" in view.title
    assert {channel.channel_name for channel in view.model.session.capture_channels} == {"capture.D15", "capture.D8"}

    assert document.open_node_data("points")  # a table: a table view
    assert any(isinstance(item, ValueDocument) for item in shell.documents())

    assert document.open_run_data()
    whole = shell.active_document()
    names = {channel.channel_name for channel in whole.model.session.capture_channels} | {
        channel.channel_name for channel in whole.model.session.analog_channels}
    assert {"capture.D15", "capture.D8", "frequency.out", "check.pass"} <= names
    assert whole.model.visible_samples == whole.model.sample_count  # the whole run at first

    # another run updates the open data views in place
    count = len(shell.documents())
    assert document.start_run()
    assert wait_for(lambda: document.runner.result is not None and document.runner.result.state == "finished")
    assert len(shell.documents()) == count and whole in shell.documents()

    document.action_show_data.trigger()  # hidden in the nodes
    assert not capture.data_entries
    document.action_show_data.trigger()
    document.clear_run_data()
    assert not capture.data_entries and not document.open_run_data()


def test_arranged_nodes_keep_room_for_their_data(shell):
    document = shell.new_flow()
    document.add_node("device.capture", (0, 0), "capture", channels=["D0"])
    item = document.scene.nodes["capture"]
    assert document.scene.node_height("capture") == item._base_size()[1] + item.reserved > item.boundingRect().height() - 4
