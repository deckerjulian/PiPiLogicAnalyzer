"""The parts of the shell: tabs with their close button on the left, the node palette beside a flow
that is edited, an activity bar that is arranged with a right click, collapsible parts of the
sidebar, the flow's overview."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTabBar, QToolButton


def settle(times: int = 10) -> None:
    for _ in range(times):
        QApplication.processEvents()


def test_tabs_close_on_their_left(shell):
    first, second = shell.new_flow(), shell.new_data_view()
    group = shell.area.group_of(first)
    bar = group.tabBar()
    settle()
    for index in range(bar.count()):
        holder = bar.tabButton(index, QTabBar.LeftSide)
        assert holder is not None and bar.tabButton(index, QTabBar.RightSide) is None
        # inside the tab, not against its edge, and centred on the text
        tab, button = bar.tabRect(index), holder.findChild(QToolButton, "tab-close")
        corner = holder.mapTo(bar, button.geometry().topLeft())
        assert corner.x() - tab.left() >= 4
        assert abs((corner.y() + button.height() / 2) - tab.center().y()) <= 2
    holder = bar.tabButton(group.indexOf(second), QTabBar.LeftSide)
    holder.findChild(QToolButton, "tab-close").click()
    settle()
    assert second not in shell.area.documents() and first in shell.area.documents()


def test_the_node_palette_shows_beside_a_flow_that_is_edited(shell):
    assert "nodes" not in shell.activity_bar.keys()  # (no section of its own)
    flow = shell.new_flow()
    settle()
    assert shell.nodes_dock.isVisible()
    flow.set_view("YAML")
    settle()
    assert not shell.nodes_dock.isVisible()  # (the text is edited, not the graph)
    flow.set_view("Graph")
    view = shell.new_data_view()
    settle()
    assert not shell.nodes_dock.isVisible()
    shell.area.activate(flow)
    settle()
    assert shell.nodes_dock.isVisible()
    shell.action_nodes.setChecked(False)  # Ctrl+Shift+N
    assert not shell.nodes_dock.isVisible()
    shell.action_nodes.setChecked(True)
    assert shell.nodes_dock.isVisible() and view in shell.area.documents()


def test_the_activity_bar_is_arranged(shell):
    bar = shell.activity_bar
    assert bar.keys() == ["project", "devices", "search"]
    assert [key for key, _action, bottom in bar.ordered() if bottom] == ["start", "settings"]
    bar.move("search", -1)
    assert [key for key, _a, bottom in bar.ordered() if not bottom] == ["project", "search", "devices"]
    bar.set_shown("devices", False)
    assert not bar.shown("devices") and bar.section_action("devices") not in bar.actions()
    bar.set_show_labels(True)
    from openscilab.ui.shell.activity_bar import ActivityBar

    again = ActivityBar()  # (kept)
    assert again.config["hidden"] == ["devices"] and again.config["labels"] is True
    for key in ("project", "search"):
        bar.set_shown(key, False)
    assert bar.shown("search") or bar.shown("project")  # one section stays
    menu = bar.context_menu("project")
    assert "Move up" in [action.text() for action in menu.actions()]
    bar.reset()
    assert bar.config == {} and bar.shown("devices")


def test_parts_of_the_sidebar_open_and_close(shell):
    section = shell.project_section
    part = section.documents_part
    assert part.is_open and not part.content.isHidden()
    part.set_open(False)
    assert part.content.isHidden()
    from openscilab.ui.shell.sections import _closed

    assert "documents" in _closed()
    part.set_open(True)
    assert "documents" not in _closed()
    shell.new_flow()
    settle()
    assert part.count_label.text() == "1"


def test_the_overview_of_a_flow_draws_its_nodes(shell):
    shell.open_example("13-time/01-align-instruments")
    settle(20)
    flow = next(document for document in shell.area.documents() if document.document_kind == "flow")
    shell.area.activate(flow)
    settle(20)
    image = flow.view.minimap.grab().toImage()
    colours = {image.pixelColor(x, y).name() for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)}
    assert len(colours) > 8  # (nodes in the colours of their kinds, wires, the view)


def test_a_message_has_no_empty_space_beside_its_icon(shell):
    from PySide6.QtWidgets import QLabel, QMessageBox

    from openscilab.ui import messages

    box = messages._box(shell, QMessageBox.Question, "Unsaved changes", "The flow was not saved. Save it?", "Details")
    box.addButton(QMessageBox.Ok)
    box.show()
    settle()
    icon = next(label for label in box.findChildren(QLabel) if not label.pixmap().isNull())
    text = box.findChild(QLabel, "qt_msgbox_label")
    assert icon.width() < 100 and text.width() >= messages.MESSAGE_WIDTH
    box.close()
