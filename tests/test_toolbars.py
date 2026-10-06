"""Tool bars that fit their window and that people arrange themselves (ui/widgets/toolbar.py): less
important entries lose their text, then move into the *more* menu, pinned ones stay; what is there
works as in the bar; the arrangement (shown, order, display) is kept per bar."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QMainWindow,
    QPushButton,
    QToolButton,
)

from openscilab.core import settings
from openscilab.ui.icons import icon
from openscilab.ui.widgets.toolbar import (
    HIGH,
    LOW,
    NORMAL,
    TOOLBARS_FILE,
    AdaptiveToolBar,
    CustomizeDialog,
)


def settle() -> None:
    for _ in range(20):
        QApplication.processEvents()


def button(text: str, name: str = "gear") -> QPushButton:
    widget = QPushButton(text)
    widget.setIcon(icon(name))
    return widget


def make_bar(qtbot, key: str = "test"):
    window = QMainWindow()
    qtbot.addWidget(window)
    bar = AdaptiveToolBar(key, "Test", window)
    run = QAction(icon("play"), "Run", window)
    bar.add_action("run", run, "run", HIGH, pinned=True)
    stop = QAction(icon("stop"), "Stop", window)
    bar.add_action("stop", stop, "run", HIGH, text=False)
    device = QComboBox()
    device.addItems(["Pico", "DSLogic"])
    bar.add_widget("device", device, "Device", "device", HIGH)
    bar.add_widget("settings", button("Settings"), "Settings", "device", NORMAL)
    fast = QCheckBox("Fast")
    bar.add_widget("fast", fast, "Fast", "device", LOW)
    bar.add_stretch()
    save = button("Save", "save")
    save.setEnabled(False)  # (nothing to save yet)
    bar.add_widget("save", save, "Save", "file", LOW)
    bar.add_widget("export", button("Export"), "Export", "file", LOW, default=False)
    bar.finish()
    window.addToolBar(bar)
    window.show()
    return window, bar, {"run": run, "stop": stop, "device": device, "fast": fast, "save": save}


def resize(window, bar, width: int) -> None:
    window.resize(width, 200)
    settle()
    bar.relayout()


def inside(bar) -> list[str]:
    return [item.key for item in bar._entries if not item.stretch and item not in bar.overflowed()]


def test_a_narrow_bar_gives_way_by_importance_and_comes_back(qtbot):
    window, bar, parts = make_bar(qtbot)
    resize(window, bar, 1000)
    assert bar.overflowed() == [] and not bar.more_button.isVisibleTo(bar)
    assert bar.button("settings").text() == "Settings"
    assert [item.key for item in bar._entries if not item.stretch] == ["run", "stop", "device", "settings", "fast",
                                                                        "save"]  # (export: not by default)
    resize(window, bar, 170)
    out = [item.key for item in bar.overflowed()]
    assert "run" in inside(bar) and bar.more_button.isVisibleTo(bar)
    assert "save" in out and "fast" in out  # the less important first
    # what went before a wide entry (the combo box) comes back once that one is in the menu, as an icon
    assert "device" in out and "settings" in inside(bar) and bar.button("settings").text() == ""
    assert bar.findChild(QToolButton, "qt_toolbar_ext_button") is None or \
        not bar.findChild(QToolButton, "qt_toolbar_ext_button").isVisibleTo(bar)
    # taken out of the bar, not hidden: the action stays usable in its menus, the widget keeps its state
    assert parts["stop"].isVisible() and parts["stop"].isEnabled() or "stop" in inside(bar)
    resize(window, bar, 1000)
    assert bar.overflowed() == []
    assert not parts["save"].isEnabled()  # (disabled by the application, it stays so)
    assert parts["run"].isEnabled() and parts["stop"].isEnabled()


def test_the_more_menu_works_as_the_bar(qtbot):
    window, bar, parts = make_bar(qtbot)
    resize(window, bar, 100)
    assert {"device", "fast", "save"} <= {item.key for item in bar.overflowed()}
    bar._fill_more()
    entries = {action.text(): action for action in bar.more_menu.actions() if not action.isSeparator()}
    assert entries["Fast"].isCheckable() and not entries["Fast"].isChecked()
    entries["Fast"].trigger()
    assert parts["fast"].isChecked()
    assert not entries["Save"].isEnabled()
    devices = entries["Device"].menu()
    assert [action.text() for action in devices.actions()] == ["Pico", "DSLogic"]
    devices.actions()[1].trigger()
    assert parts["device"].currentText() == "DSLogic"
    assert "Customize toolbar..." in entries
    if "Stop" in entries:  # an action of the bar: itself, enabled
        assert entries["Stop"] is parts["stop"] and entries["Stop"].isEnabled()


def test_the_arrangement_is_kept_per_bar(qtbot):
    window, bar, _parts = make_bar(qtbot)
    resize(window, bar, 1000)
    order = ["save", "run", "stop", "device", "settings", "fast", bar.items[5].key, "export"]
    bar.set_arrangement(order, {"save", "run", "stop", "device", "export"}, "icons")
    assert [item.key for item in bar._entries if not item.stretch] == ["save", "run", "stop", "device", "export"]
    assert bar.button("export").text() == ""  # icons only
    stored = settings.get_settings(TOOLBARS_FILE)["test"]
    assert stored["hidden"] == ["settings", "fast"] and stored["shown"] == ["export"] and stored["mode"] == "icons"
    # a new bar of the same kind arranges itself so
    window2, bar2, _parts2 = make_bar(qtbot)
    resize(window2, bar2, 1000)
    assert [item.key for item in bar2._entries if not item.stretch] == ["save", "run", "stop", "device", "export"]
    bar2.set_mode("text")
    assert bar2.button("stop").toolButtonStyle() != bar2.button("stop").toolButtonStyle().ToolButtonIconOnly
    bar2.reset()
    assert "test" not in (settings.get_settings(TOOLBARS_FILE) or {})
    assert bar2._entries[0].key == "run"


def test_the_customize_dialog_and_the_context_menu(qtbot):
    window, bar, _parts = make_bar(qtbot)
    dialog = CustomizeDialog(bar, window)
    order, visible, mode = dialog.arrangement()
    assert order[0] == "run" and "export" not in visible and "save" in visible and mode == "auto"
    menu = bar.context_menu()
    texts = [action.text() for action in menu.actions()]
    assert "Customize toolbar..." in texts and "Reset toolbar" in texts
    assert any(text.startswith("Automatic") for text in texts) and "Icons only" in texts


def test_an_entry_that_does_not_apply_is_left_out(qtbot):
    window, bar, _parts = make_bar(qtbot)
    resize(window, bar, 1000)
    bar.set_available("settings", False)
    assert "settings" not in inside(bar) and bar.overflowed() == []
    bar.set_available("settings", True)
    assert "settings" in inside(bar)


def test_the_bars_of_the_application_fit_a_small_window(shell):
    shell.resize(950, 700)
    flow = shell.new_flow()
    settle()
    bar = flow.toolbar()
    keys = inside(bar)
    assert "run" in keys and "view-graph" in keys and "stop" in keys
    assert {item.key for item in bar.overflowed()} >= {"zoom-in", "zoom-out"}
    data_view = shell.new_data_view()
    settle()
    assert "capture" in inside(data_view.main_toolbar)
    assert "fit" in inside(data_view.view_toolbar)


def test_a_dialog_of_a_tool_bar_has_the_background_of_a_dialog(shell, monkeypatch):
    """A dialog that is a child of a tool bar took over its transparent background (it showed white)."""
    from PySide6.QtWidgets import QDialog

    from openscilab.driver.simulated import open_simulated
    from openscilab.ui.theme import BACKGROUND, build_stylesheet

    assert "QToolBar QDialog {\n    background-color: " + BACKGROUND in build_stylesheet()
    instrument = open_simulated("pico")
    shell.hub.add(instrument)
    view = shell.show_data(instrument)
    parents = []
    monkeypatch.setattr(QDialog, "exec", lambda self: parents.append(self.parentWidget()) or 0)
    view.capture_controls.capture_settings()
    assert parents and parents[0] is view.window()  # not the tool bar
