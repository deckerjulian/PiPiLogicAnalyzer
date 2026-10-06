"""Starting a project: every project of the example library is a starting point - the start page
shows them as tiles by category (the first category holds the plain ones), opening one copies it
into a temporary project. The Logic Analyzer is one of them, a project that opens a data view."""

from __future__ import annotations

import os

from openscilab.lab import examples, templates
from openscilab.lab.project import Project
from openscilab.ui.documents.dataview import DataView

START = ["00-start/01-empty-lab", "00-start/02-logic-analyzer", "00-start/03-data-acquisition",
         "00-start/04-synchronized-instruments", "00-start/05-remote-measuring-device"]
BENCH = "05-measurement/07-test-bench"
LOGGER = "08-data/07-long-term-logger"
DECODERS = os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "decoders", "uart"))


def test_the_first_category_holds_the_starting_points():
    first = examples.catalog()[0]
    assert first.key == "00-start" and first.title == "Start a project"
    assert [example.key for example in first.examples] == START
    assert not os.path.exists(os.path.join(os.path.dirname(__file__), "..", "examples", "templates"))
    logic = examples.find("00-start/02-logic-analyzer")
    assert logic.icon == "channels" and templates.views_to_open(logic.path) == ["data"]
    assert templates.files_to_open(logic.path) == []


def test_the_logic_analyzer_is_a_project_that_opens_a_data_view(shell):
    from openscilab.driver.simulated import open_simulated

    view = shell.open_example("00-start/02-logic-analyzer")
    assert isinstance(view, DataView) and view.instrument is None
    assert shell.activity_bar.current == "devices"  # (no device: the device list shows)
    instrument = open_simulated("pico")
    shell.hub.add(instrument)
    view = shell.open_example("00-start/02-logic-analyzer")
    assert view.instrument is instrument and not view.viewer


def test_the_start_page_shows_every_project_as_a_tile_by_category(shell):
    page = shell.show_start_page()
    assert [section.category.title for section in page.sections][:2] == ["Start a project", "Basics"]
    assert set(page.tiles) == {example.key for example in examples.examples()}
    tile = page.tiles["00-start/03-data-acquisition"]
    assert tile.footer.text() == "sim:daq · real time" and tile.accessibleName() == "Data acquisition"
    assert page.apply_filter("sync signal") >= 2
    assert not page.tiles["00-start/04-synchronized-instruments"].isHidden()
    assert page.tiles["00-start/01-empty-lab"].isHidden() and not page.sections[0].isHidden()
    assert page.apply_filter("no such thing anywhere") == 0 and not page.no_match.isHidden()
    page.apply_filter("")
    assert all(not tile.isHidden() for tile in page.tiles.values())
    opened = []
    page.example_requested.connect(opened.append)
    page.tiles["00-start/01-empty-lab"].click()
    assert opened == ["00-start/01-empty-lab"]
    assert any(document.document_kind == "flow" for document in shell.area.documents())  # (the shell opened it)
    assert shell.new_project_dialog() is page and shell.active_document() is page


def test_the_test_bench_report(tmp_path):
    import pytest

    from openscilab.lab.engine import Engine
    from openscilab.lab.flow_files import load_flow

    if not DECODERS:
        pytest.skip("the sigrok decoders are not installed in ./decoders")
    root = templates.copy_project(examples.find(BENCH).path, str(tmp_path / "bench"))
    project = Project.open(root)
    flow = project.complete(load_flow(os.path.join(root, "flows", "test.flow.yaml")))
    assert Engine(flow, mode="virtual", project=project).run(timeout=60).ok
    with open(os.path.join(root, "data", "test-report.html"), encoding="utf-8") as handle:
        text = handle.read()
    assert "Clock on D15: 1 kHz" in text and "openSciLab" in text and "<span class='pass'>PASSED</span>" in text


def test_the_test_bench_passes_in_real_time(tmp_path):
    """The UART text repeats: a capture that starts anywhere in it still finds the text."""
    import pytest

    from openscilab.lab.engine import Engine
    from openscilab.lab.flow_files import load_flow

    if not DECODERS:
        pytest.skip("the sigrok decoders are not installed in ./decoders")
    root = templates.copy_project(examples.find(BENCH).path, str(tmp_path / "bench"))
    project = Project.open(root)
    flow = project.complete(load_flow(os.path.join(root, "flows", "test.flow.yaml")))
    for _run in range(3):  # each run starts somewhere else in the text
        engine = Engine(flow, mode="real", project=project)
        result = engine.run(timeout=60)
        assert result.ok, result.error
        assert [item for item in engine.views.items if item[0] == "check" and not item[2]] == []


def test_the_logger_writes_its_file(tmp_path):
    from openscilab.lab.engine import Engine
    from openscilab.lab.flow_files import load_flow

    root = templates.copy_project(examples.find(LOGGER).path, str(tmp_path / "logger"))
    project = Project.open(root)
    flow = project.complete(load_flow(os.path.join(root, "flows", "logger.flow.yaml")))
    assert Engine(flow, mode="virtual", project=project, duration=2.0).run(timeout=60).ok
    with open(os.path.join(root, "data", "log.csv"), encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert len(lines) > 20  # 10 reports a second of two values, and the header


# ------------------------------------------------------------- in the shell
def test_a_project_opens_as_a_temporary_project_and_is_saved_later(shell, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    document = shell.open_example("00-start/03-data-acquisition")  # no folder asked
    assert document is not None and shell.is_temporary(document)
    root = shell.project_root_of(document)
    import tempfile

    assert os.path.realpath(root).startswith(os.path.realpath(tempfile.gettempdir()))
    flow_document = next(doc for doc in shell.area.documents() if doc.document_kind == "flow")
    flow_document._edit("rename", lambda flow: setattr(flow, "description", "Changed"))
    assert flow_document.dirty

    shell.area.activate(flow_document)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(tmp_path / "My lab"), ""))
    assert shell.save_active()
    kept = str(tmp_path / "My lab")  # the user names the project folder
    assert flow_document.path == os.path.join(kept, "flows", "daq.flow.yaml") and not flow_document.dirty
    assert "Changed" in open(flow_document.path, encoding="utf-8").read()
    assert document.path == os.path.join(kept, "panels", "daq.panel.yaml")  # the panel moved along
    assert not shell.is_temporary(document) and flow_document.project.root == kept
    assert shell.data_folder(flow_document) == os.path.join(kept, "data")


# ------------------------------------------------------------ the user's own
def test_a_project_is_kept_as_a_template_of_the_user(tmp_path):
    root = templates.copy_project(examples.find("00-start/01-empty-lab").path, str(tmp_path / "bench"))
    with open(os.path.join(root, "data", "capture.lac"), "w") as handle:
        handle.write("results")
    flow = os.path.join(root, "flows", "main.flow.yaml")
    template = examples.save_template(root, "Sensor  bench", "For the sensors\nof the lab", [flow, "/elsewhere/x"])
    assert template.key == "my-templates/sensor-bench" and template.user and template.name == "sensor-bench"
    first = examples.catalog()[0]
    assert first.key == examples.USER_CATEGORY and first.title == "My templates"
    assert [example.title for example in first.examples] == ["Sensor bench"]
    assert examples.catalog()[1].key == "00-start"
    saved = Project.open(template.path)
    assert saved.name == "Sensor bench" and saved.extra["description"] == "For the sensors of the lab"
    assert saved.extra["open"] == ["flows/main.flow.yaml"] and saved.devices == {"sim": "sim:free"}
    assert os.listdir(os.path.join(template.path, "data")) == []  # (no results of runs)
    assert examples.save_template(root, "Sensor bench").key == "my-templates/sensor-bench-2"
    examples.delete_template(template.key)
    assert [example.key for example in examples.catalog()[0].examples] == ["my-templates/sensor-bench-2"]
    for key in ("00-start/01-empty-lab", "my-templates/../settings", "my-templates/"):
        try:
            examples.delete_template(key)
        except ValueError:
            continue
        raise AssertionError(f"{key} was deleted")


def test_the_shell_saves_a_project_as_a_template_and_lists_it_first(shell, monkeypatch):
    from openscilab.ui import messages
    from openscilab.ui.dialogs.template_dialog import TemplateDialog

    document = shell.open_example("00-start/01-empty-lab")
    document._edit("rename", lambda flow: setattr(flow, "description", "Mine"))

    def named(dialog):
        assert dialog.title == "Empty lab"  # (the project's name to start from)
        dialog.title_edit.setText("My bench")
        dialog._accept()
        return dialog.result()

    monkeypatch.setattr(TemplateDialog, "exec", named)
    monkeypatch.setattr(messages, "confirm", lambda *args, **kwargs: True)  # (save the changed flow first)
    key = shell.save_as_template()
    assert key == "my-templates/my-bench" and not document.dirty
    menus = [action.menu().title() for action in shell.examples_menu.actions() if action.menu()]
    assert menus[:2] == ["My templates", "Start a project"]
    page = shell.show_start_page()
    assert page.sections[0].category.title == "My templates" and key in page.tiles
    assert [action.text() for action in page.tiles[key].context_menu().actions()][-1] == "Delete template..."
    assert len(page.tiles["00-start/01-empty-lab"].context_menu().actions()) == 1  # (the library's stay)

    opened = shell.open_example(key)  # a new project from it, with the flow as it was saved
    assert opened.document_kind == "flow" and opened.flow.description == "Mine" and shell.is_temporary(opened)

    page.tiles[key].context_menu().actions()[-1].trigger()  # Delete template...
    assert key not in page.tiles and page.sections[0].category.title == "Start a project"
    assert "My templates" not in [action.menu().title() for action in shell.examples_menu.actions() if action.menu()]
