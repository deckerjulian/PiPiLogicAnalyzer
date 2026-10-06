"""Completions in the YAML view of a flow, in Python code of a node and in the console."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from openscilab.lab import completion, hints, yaml_io

FLOW = """flow: Pwm
nodes:
  uno: {type: device.instrument, address: sim:uno}
  pwm: {type: gpio.pwm, pin: D9}
  mean: {type: measure.mean}
  capture: {type: device.capture, channels: [A0]}
edges:
  - uno.device -> pwm.device
  - uno.device -> capture.device
  - capture.A0 -> mean.in
"""


def offered(text: str, **kwargs) -> list[str]:
    flow = yaml_io.loads(FLOW)
    _start, items = completion.yaml_completions(text, flow, devices=hints.flow_devices(flow), **kwargs)
    return [item.inserted for item in items]


# ---------------------------------------------------------------------- YAML
def test_sections_and_node_types():
    assert offered("ed") == ["edges: "]
    assert offered("nodes:\n  x: {type: gpio.p")[:2] == ["gpio.pulse", "gpio.pwm"]
    assert "gpio.pwm" in offered("nodes:\n  x: {type: pwm")  # also found by a part of the name
    assert "gpio.pwm" in offered("nodes:\n  x:\n    type: gpio.pw")


def test_parameters_of_the_node_type_that_are_not_there_yet():
    keys = offered("nodes:\n  pwm: {type: gpio.pwm, pin: D9, ")
    assert "freq: " in keys and "duty: " in keys and "pin: " not in keys
    assert offered("nodes:\n  check:\n    type: report.check\n    name: x\n    exp") == ["expected: "]


def test_values_from_the_device_and_the_choices():
    assert offered("nodes:\n  pwm: {type: gpio.pwm, pin: D") == ["D3", "D5", "D6", "D9", "D10", "D11"]
    # a node typed just now (not read yet, not wired): the only device of the flow
    assert offered("nodes:\n  led: {type: gpio.write, pin: D1")[:3] == ["D10", "D11", "D12"]
    assert "A1" in offered("nodes:\n  capture: {type: device.capture, channels: [A0, ")
    assert offered("nodes:\n  m: {type: measure.pulse_width, level: ") == ["high", "low"]
    assert offered("nodes:\n  c: {type: device.stream, until_stopped: ") == ["true", "false"]
    assert offered("nodes:\n  d: {type: device.instrument, address: sim:u",
                   addresses=[("Uno", "sim:uno"), ("Free", "sim:free")]) == ["sim:uno"]


def test_ports_in_the_edges():
    assert "capture.A0 -> " in offered("edges:\n  - capture.")
    inputs = offered("edges:\n  - capture.A0 -> mean.")
    assert inputs == ["mean.in"]
    assert "pwm.duty" in offered("edges:\n  - capture.A0 -> ")


# -------------------------------------------------------------------- Python
def test_python_attributes_and_port_names():
    def names(text: str) -> list[str]:
        return [item.text for item in completion.python_completions(text, inputs=["table", "done"],
                                                                    outputs=["slope"], devices=["uno"])[1]]

    assert "emit" in names("ctx.") and names("ctx.em") == ["emit"]
    assert names('ctx.emit("') == ["slope"] and names("ctx.receive('t") == ["table"]
    assert names('    if port == "d') == ["done"]
    assert names('ctx.device("') == ["uno"]
    assert "Scalar" in names("signals.Sc") and "linspace" in names("np.lins")
    assert "signals" in names("sig") and "async" in names("asy")
    assert "slope_value" in names("slope_value = 1\nslope_v")  # names of the code itself
    assert names("def ru") == []  # a new name: nothing to complete


# ----------------------------------------------------------------------- UI
def _settle() -> None:
    for _ in range(5):
        QApplication.processEvents()


def test_completing_in_the_yaml_view(shell):
    document = shell.new_flow()
    document.set_view("YAML")
    edit = document.yaml_edit
    edit.setPlainText("flow: Flow\nnodes:\n  ")
    cursor = edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    edit.setFocus()
    QTest.keyClicks(edit, "x: {type: gpio.pw")
    _settle()
    assert document.yaml_completer.visible and document.yaml_completer.items()[0] == "gpio.pwm"
    QTest.keyClick(edit, Qt.Key_Return)
    _settle()
    assert edit.toPlainText().endswith("x: {type: gpio.pwm")
    QTest.keyClicks(edit, "}")
    assert document.apply_yaml() and document.flow.nodes["x"].type == "gpio.pwm"


def test_completing_in_the_code_of_a_python_node(shell):
    document = shell.new_flow()
    document.add_node("control.python", (0, 0), "fit", inputs=["table"], outputs=["slope"])
    document.scene.select_nodes(["fit"])
    edit = document.inspector_widget().editors["code"]
    edit.setPlainText("")
    edit.setFocus()
    QTest.keyClicks(edit, 'ctx.emit("s')
    _settle()
    assert edit.completer.visible and edit.completer.items() == ["slope"]
    QTest.keyClick(edit, Qt.Key_Tab)
    assert edit.toPlainText() == 'ctx.emit("slope'


def test_completing_in_the_console(shell):
    console = shell.console.python
    console.input.setFocus()
    QTest.keyClicks(console.input, "shell.new_fl")
    _settle()
    assert "new_flow" in console.completer.items()
    QTest.keyClick(console.input, Qt.Key_Return)  # takes the completion, does not run the line
    assert console.input.text() == "shell.new_flow"
