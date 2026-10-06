"""Documents of the view nodes: charts, numbers, LEDs, logs and tables, fed by a running flow."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from openscilab.core import signals
from openscilab.lab import Flow
from openscilab.ui.documents.chart import ChartDocument, ValueDocument, nice_ticks


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def test_nice_ticks():
    assert nice_ticks(0, 10) == [0, 2, 4, 6, 8, 10]
    assert nice_ticks(0.0, 0.0011, 5)[:3] == [0.0, 0.00025, 0.0005]


def test_a_running_flow_opens_its_views(shell):
    flow = Flow("Views")
    flow.add_node("control.sweep", "sweep", start=0, stop=4, step=1, dwell="10 ms")
    flow.add_node("dsp.math", "square", expression="a * a", unit="V")
    flow.connect("sweep.value", "square.a")
    for kind in ("number", "led", "log", "table", "strip_chart"):
        flow.add_node(f"view.{kind}", kind, title=kind.title())
        flow.connect("square.out", f"{kind}.in")
    flow.add_node("view.xy", "xy", title="Curve")
    flow.connect("sweep.value", "xy.x")
    flow.connect("square.out", "xy.y")
    document = shell.new_flow(flow)
    document.fast_box.setChecked(True)
    assert document.start_run()
    assert wait_for(lambda: document.runner.result is not None)
    assert wait_for(lambda: len(shell.documents()) == 7)

    views = {doc.title: doc for doc in shell.documents() if isinstance(doc, (ChartDocument, ValueDocument))}
    assert set(views) == {"Number", "Led", "Log", "Table", "Strip_Chart", "Curve"}
    assert views["Number"].label.text() == "16 V"
    assert views["Led"].led.on
    assert views["Log"].text.toPlainText().count("\n") == 4
    assert views["Table"].table.rowCount() == 1  # the rows of the last value
    curve = views["Curve"].chart
    assert curve.series["y"] == ([0.0, 1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 4.0, 9.0, 16.0])
    for view in views.values():
        view.resize(500, 300)
        assert not view.grab().isNull()


def test_a_spectrum_document_draws(application=None):
    QApplication.instance() or QApplication([])
    document = ChartDocument("spectrum", "Spectrum")
    document.show_value(signals.Table(columns={"frequency": [0.0, 100.0, 200.0], "magnitude": [-60.0, -3.0, -40.0]}), {})
    assert document.chart.bounds()[0:2] == (0.0, 200.0)
    document.resize(400, 300)
    assert not document.grab().isNull()
