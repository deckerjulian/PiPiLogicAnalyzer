"""Reports: sections, tables, images and checks into HTML (and PDF), the same headless with
``openscilab run --report``; the template *Characteristic curve*."""

from __future__ import annotations

import os
import shutil

import pytest

from openscilab.lab import templates, yaml_io
from openscilab.lab.engine import Engine
from openscilab.lab.project import Project
from openscilab.lab.report import svg_plot

FLOW = """
flow: Report
nodes:
  sweep: {type: control.sweep, start: 1, stop: 4, step: 1}
  square: {type: dsp.math, expression: "a * a"}
  points: {type: data.table, columns: [x, y]}
  intro: {type: report.section, title: Introduction, text: "Squares of 1 to 4."}
  values: {type: report.table, title: Values}
  diagram: {type: report.image, title: Squares, x: x, y: y}
  check: {type: report.check, name: Last square, low: 10, high: 20}
  strict: {type: report.check, name: Exactly nine, expected: 9}
  write: {type: report.write, path: out.html, title: Squares}
edges:
  - sweep.value -> square.a
  - sweep.value -> points.x
  - square.out -> points.y
  - points.table -> values.in
  - points.table -> diagram.in
  - square.out -> check.in
  - square.out -> strict.in
"""


def run(text: str, tmp_path) -> Engine:
    flow = yaml_io.loads(text)
    engine = Engine(flow, mode="virtual", base_dir=str(tmp_path), data_dir=str(tmp_path))
    result = engine.run(timeout=20)
    assert result.ok, result.error
    return engine


def test_an_html_report_with_a_table_an_image_and_checks(tmp_path):
    engine = run(FLOW, tmp_path)
    text = (tmp_path / "out.html").read_text()
    assert "<h1>Squares</h1>" in text and "Squares of 1 to 4." in text
    assert text.index("Introduction") < text.index("Values") < text.index("<svg")  # in the order of the flow
    assert text.count("<polyline") == 1 and "<td>16</td>" in text
    assert "Last square: 16 ≥ 10, ≤ 20" in text and "class='pass'>PASSED" in text
    assert "class='fail'>FAILED" in text  # the strict check fails for 1, 4 and 16
    assert "openSciLab report" in text and "<span class='fail'>FAILED</span>" in text  # the verdict
    assert engine.values[("write", "passed")].value is False


def test_the_same_report_headless_from_the_command_line(tmp_path):
    from openscilab import cli

    path = tmp_path / "flow.flow.yaml"
    path.write_text(FLOW)
    report = tmp_path / "cli.html"
    assert cli.main(["run", str(path), "--fast", "--report", str(report)]) == 1  # a check failed
    text = report.read_text()
    assert "Last square" in text and "<svg" in text and "<h2>Run</h2>" in text


def test_a_pdf_report(tmp_path):
    pytest.importorskip("PySide6.QtGui")
    flow = FLOW.replace("path: out.html", "path: out.pdf")
    run(flow, tmp_path)
    data = (tmp_path / "out.pdf").read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 1000
    assert b"/Image" in data  # the diagram is in it (rich text of Qt draws no inline SVG)


def test_diagrams_become_images_for_the_pdf(qtbot):
    from PySide6.QtGui import QTextDocument

    from openscilab.ui.report_pdf import embed_diagrams

    document = QTextDocument()
    svg = svg_plot([("a", [0, 1, 2], [0, 1, 4])])
    text = embed_diagrams(document, f"<h2>Curve</h2>{svg}<p>after</p>{svg}")
    assert "<svg" not in text and text.count("<img src='diagram-") == 2 and "<p>after</p>" in text
    image = document.resource(QTextDocument.ImageResource, "diagram-1.png")
    assert not image.isNull() and image.width() == 1280
    # something is drawn in it: not every pixel is white
    assert any(image.pixel(x, y) != 0xFFFFFFFF for x in range(0, image.width(), 7) for y in range(0, image.height(), 7))


def test_svg_plots():
    svg = svg_plot([("a", [0, 1, 2], [0, 1, 4]), ("b", [0, 2], [1, 1])], x_label="x", y_label="y")
    assert svg.count("<polyline") == 2 and ">x</text>" in svg
    assert "no data" in svg_plot([])


def test_the_characteristic_curve_example(tmp_path):
    """Acceptance: the example (a template before) measures the curve, checks its slope and writes the report."""
    from openscilab.lab import examples

    source = examples.find("05-measurement/06-characteristic-curve").path
    root = templates.copy_project(source, str(tmp_path / "curve"))
    assert sorted(os.path.basename(path) for path in templates.files_to_open(root)) == [
        "curve.flow.yaml", "curve.panel.yaml"]
    project = Project.open(root)
    flow = project.complete(yaml_io.load(os.path.join(root, "flows", "curve.flow.yaml")))
    engine = Engine(flow, mode="virtual", project=project)
    result = engine.run(timeout=30)
    assert result.ok, result.error
    assert engine.values[("fit", "slope")].value == pytest.approx(5.0, abs=0.1)
    with open(os.path.join(project.data_dir, "curve.html"), encoding="utf-8") as handle:
        text = handle.read()
    assert "<svg" in text and "Slope: " in text and "<span class='pass'>PASSED</span>" in text
    with pytest.raises(FileExistsError):
        shutil.copy(os.path.join(root, "project.yaml"), os.path.join(root, "data", "x"))
        templates.copy_project(source, os.path.join(root, "data"))
