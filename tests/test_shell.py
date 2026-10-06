"""The openSciLab shell: documents, tab groups, command palette, start page, layouts."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from openscilab.core import fuzzy, recent
from openscilab.ui.documents.dataview import DataView
from openscilab.ui.shell import command_palette
from openscilab.ui.shell.start_page import StartPage

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEMO = os.path.join(ROOT, "examples", "demo.lac")


def menu_titles(shell) -> list[str]:
    return [action.text().replace("&", "") for action in shell.menuBar().actions()]


# ------------------------------------------------------------------ start up
def test_the_shell_starts_with_its_menus_and_docks(shell):
    assert menu_titles(shell) == ["Project", "Edit", "View", "Devices", "Templates", "Help"]
    assert shell.sidebar_dock.isVisible()
    assert shell.inspector_dock.isVisible()
    assert not shell.console_dock.isVisible()
    assert shell.active_document() is None
    assert shell.windowTitle().startswith("openSciLab")


def test_the_logic_analyzer_template_opens_devices_and_an_analyzer(shell):
    document = shell.open_example("00-start/02-logic-analyzer")

    assert isinstance(document, DataView)
    assert shell.active_document() is document
    assert shell.activity_bar.current == "devices"
    assert shell.sidebar.current_key == "devices"
    # The menus of the data view join the shell's; devices are the shell's business.
    assert menu_titles(shell) == ["Project", "Edit", "Data", "Analyze", "Display", "View", "Devices", "Templates", "Help"]
    assert not document.menuBar().isVisible()
    assert document.toolbar() is not None


# ---------------------------------------------------------------- documents
def test_opening_a_capture_creates_an_analyzer_document(shell):
    document = shell.open_file(DEMO)

    assert isinstance(document, DataView)
    assert document.title == "demo.lac"
    assert document.model.sample_count > 0
    assert not document.dirty
    assert recent.entries("files") == [os.path.abspath(DEMO)]
    # Opening it again shows the open document.
    assert shell.open_file(DEMO) is document
    assert len(shell.documents()) == 1


def test_a_document_with_changes_is_marked_and_asks_before_closing(shell, monkeypatch):
    from openscilab.ui import messages

    document = shell.open_file(DEMO)
    document.delete_samples(0, 10)
    group = shell.area.group_of(document)

    assert document.dirty
    assert group.tabText(group.indexOf(document)).endswith("●")
    assert shell.saved_label.text() == "1 unsaved"

    asked = []
    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: asked.append(args[2]) or None)
    assert not shell.close_active()  # cancelled
    assert document in shell.documents()

    monkeypatch.setattr(messages, "choose", lambda *args, **kwargs: 1)  # discard
    assert shell.close_active()
    assert shell.documents() == []
    assert asked and "demo.lac" in asked[0]
    assert menu_titles(shell) == ["Project", "Edit", "View", "Devices", "Templates", "Help"]


def test_save_goes_to_the_active_document(shell, tmp_path, monkeypatch):
    from openscilab.ui.documents import dataview as analyzer

    document = shell.open_file(DEMO)
    document.delete_samples(0, 10)
    target = str(tmp_path / "copy.lac")
    monkeypatch.setattr(analyzer.QFileDialog, "getSaveFileName", lambda *a, **k: (target, ""))

    assert shell.save_active_as()
    assert document.path == target and not document.dirty
    assert os.path.exists(target)
    assert recent.entries("files")[0] == target


def test_each_document_has_its_own_menus(shell):
    first = shell.open_file(DEMO)
    second = shell.new_data_view()

    assert shell.active_document() is second
    assert second.data_menu.menuAction() in shell.menuBar().actions()
    assert first.data_menu.menuAction() not in shell.menuBar().actions()

    shell.area.activate(first)
    assert first.data_menu.menuAction() in shell.menuBar().actions()
    assert second.data_menu.menuAction() not in shell.menuBar().actions()


def test_shortcuts_reach_the_active_document_only(shell):
    from PySide6.QtTest import QTest

    first = shell.open_file(DEMO)
    second = shell.new_data_view()
    second.load_session(first.model.session)
    fired = []
    for document, name in ((first, "first"), (second, "second")):
        fit = next(action for action in document.view_menu.actions() if action.text() == "Zoom to &fit")
        fit.triggered.connect(lambda _checked=False, name=name: fired.append(name))

    for document in (first, second, first):
        shell.area.activate(document)
        QApplication.processEvents()
        QTest.keyClick(shell, Qt.Key_0, Qt.ControlModifier)
        QApplication.processEvents()

    assert fired == ["first", "second", "first"]


# ---------------------------------------------------------- groups, windows
def test_documents_split_into_groups_and_back(shell):
    first = shell.open_file(DEMO)
    second = shell.new_data_view()

    shell.area.split(second, Qt.Horizontal)
    assert len(shell.area.groups()) == 2
    assert shell.area.group_of(first) is not shell.area.group_of(second)
    assert shell.active_document() is second

    shell.area.split(first, Qt.Vertical)  # the first group becomes empty and is removed
    assert len(shell.area.groups()) == 2

    shell.area.move_to_next_group(second)
    assert len(shell.area.groups()) == 1
    assert set(shell.documents()) == {first, second}


def test_a_document_can_be_detached_and_returns_when_its_window_closes(shell):
    document = shell.open_file(DEMO)

    window = shell.area.detach(document)
    assert shell.area.is_detached(document)
    assert window.centralWidget() is document
    assert document in shell.documents()

    window.close()
    QApplication.processEvents()
    assert not shell.area.is_detached(document)
    assert shell.area.group_of(document) is not None


# ----------------------------------------------------------- command palette
def test_fuzzy_matching():
    assert fuzzy.score("cap", "Capture › Start") > fuzzy.score("cap", "Analyze › Charts and maps")
    assert fuzzy.score("cst", "Capture › Start") is not None
    assert fuzzy.score("xyz", "Capture › Start") is None
    assert fuzzy.score("", "anything") == 0
    ranked = fuzzy.rank("zoom", ["View › Zoom in", "Help › About", "Waveform › Zoom to fit"], key=str)
    assert ranked == ["View › Zoom in", "Waveform › Zoom to fit"]


def test_recently_used_commands_come_first():
    commands = [command_palette.Command(name, lambda: None) for name in ("A one", "B two", "C three")]
    command_palette.remember(commands[2])

    assert [command.title for command in command_palette.search(commands, "")][0] == "C three"


def test_the_palette_finds_and_runs_every_menu_action(shell):
    document = shell.open_file(DEMO)
    titles = {command.title for command in shell.commands()}

    # Every menu entry of the shell and the analyzer can be reached.
    for expected in ("Project › Open file", "Data › Capture again", "Analyze › Measurements",
                     "Display › Zoom to fit", "View › Console", "Help › About openSciLab",
                     "Devices › Install or update firmware", "Go to › demo.lac"):
        assert expected in titles, expected

    palette = shell.show_command_palette()
    palette.input.setText("display zoom fit")
    assert palette.visible_commands()[0].title == "Display › Zoom to fit"

    document.model.set_view(0, 10)
    palette.execute_current()
    assert document.model.visible_samples == document.model.sample_count
    assert command_palette.history()[0] == "Display › Zoom to fit"


def test_the_palette_toggles_the_console(shell):
    palette = shell.show_command_palette()
    palette.input.setText("view console")
    palette.execute_current()
    assert shell.console_dock.isVisible()


# ------------------------------------------------------------------ start page
def test_the_start_page_opens_recent_files_and_the_projects_of_the_library(shell, monkeypatch):
    recent.add(DEMO)
    page = shell.show_start_page()
    assert isinstance(page, StartPage)
    assert shell.show_start_page() is page  # only one

    page.refresh()
    assert page.recent_list.count() == 1
    item = page.recent_list.item(0)
    page.recent_list.itemActivated.emit(item)
    assert isinstance(shell.active_document(), DataView)
    assert shell.active_document().title == "demo.lac"

    page.tiles["00-start/02-logic-analyzer"].click()
    assert len([d for d in shell.documents() if isinstance(d, DataView)]) == 2


def test_the_start_page_open_button_asks_for_a_file(shell, monkeypatch):
    from openscilab.ui.shell import main_window

    monkeypatch.setattr(main_window.QFileDialog, "getOpenFileName", lambda *a, **k: (DEMO, ""))
    page = shell.show_start_page()
    page.open_button.click()
    assert shell.active_document().title == "demo.lac"


def test_command_line_files_open_on_start(monkeypatch, application_for_app):
    from openscilab import app

    opened = []
    monkeypatch.setattr("openscilab.ui.shell.main_window.ShellWindow.open_file",
                        lambda self, path: opened.append(path))
    monkeypatch.setattr(application_for_app, "exec", lambda: 0)
    assert app.main([DEMO]) == 0
    assert opened == [DEMO]


def test_the_application_starts_with_a_flow(monkeypatch, application_for_app):
    from openscilab import app
    from openscilab.ui.documents.flow import FlowDocument
    from openscilab.ui.shell.main_window import ShellWindow

    windows = []
    original = ShellWindow.show
    monkeypatch.setattr(ShellWindow, "show", lambda self: (windows.append(self), original(self)))
    monkeypatch.setattr(application_for_app, "exec", lambda: 0)
    assert app.main([]) == 0
    window = windows[0]
    try:
        kinds = [document.document_kind for document in window.area.documents()]
        assert kinds == ["start", "flow"]
        assert window.active_document().document_kind == "start"  # what to do first
        flow = window.area.documents()[1]
        assert isinstance(flow, FlowDocument) and flow.path is None  # temporary until it is saved
    finally:
        window.force_close()


@pytest.fixture
def application_for_app(monkeypatch):
    """``app.main`` creates its QApplication; hand it the one of the tests instead."""
    from openscilab import app

    instance = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app, "QApplication", lambda *args: instance)
    monkeypatch.setattr(instance, "setStyle", lambda *args: None)
    # restyling every widget the earlier tests left behind takes long and tests nothing here
    monkeypatch.setattr(instance, "setStyleSheet", lambda *args: None)
    return instance


# --------------------------------------------------------------- sidebar etc.
def test_the_activity_bar_switches_and_hides_the_sidebar(shell):
    shell.activity_bar.section_action("search").trigger()
    assert shell.sidebar.current_key == "search"
    assert shell.sidebar_dock.isVisible()

    shell.activity_bar.section_action("search").trigger()  # again: hide
    assert not shell.sidebar_dock.isVisible()

    shell.toggle_sidebar()
    assert shell.sidebar_dock.isVisible()


def test_the_search_section_runs_commands(shell):
    shell.activity_bar.select("search")
    shell.search_section.input.setText("console")
    results = shell.search_section.results
    assert results.count() > 0
    results.itemActivated.emit(results.item(0))
    assert shell.console_dock.isVisible()


def test_the_project_section_lists_open_documents(shell):
    first = shell.open_file(DEMO)
    shell.new_data_view()
    titles = [shell.project_section.documents.item(i).text()
              for i in range(shell.project_section.documents.count())]
    assert titles == ["demo.lac", "Data view"]

    shell.project_section.documents.itemClicked.emit(shell.project_section.documents.item(0))
    assert shell.active_document() is first


def test_layouts_are_saved_and_restored(shell):
    shell.save_layout("wide")
    shell.toggle_inspector()
    assert not shell.inspector_dock.isVisible()

    assert shell.restore_layout("wide")
    assert shell.inspector_dock.isVisible()

    shell.delete_layout("wide")
    assert not shell.restore_layout("wide")


def test_the_python_console_sees_the_shell(shell):
    shell.open_file(DEMO)
    output = shell.console.python.execute("print(active().title)")
    assert output.strip() == "demo.lac"
    shell.console.python.execute("def f():")
    assert shell.console.python.prompt.text() == "..."


def test_status_messages_reach_the_log(shell):
    shell.statusBar().showMessage("hello lab")
    assert "hello lab" in shell.console.log.toPlainText()


def test_the_inspector_shows_the_capture_of_the_active_analyzer(shell):
    document = shell.open_file(DEMO)
    text = shell.inspector.content.text()
    assert "demo.lac" in text and "Total samples" in text

    shell.new_data_view()
    assert "not saved" in shell.inspector.content.text()
    shell.area.activate(document)
    assert "demo.lac" in shell.inspector.content.text()
