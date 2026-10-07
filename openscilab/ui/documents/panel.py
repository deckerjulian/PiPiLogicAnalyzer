# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The panel document: a measuring station's front panel bound to a flow.

*Edit*: widgets from the palette onto the surface of the panel, placed freely. Moved and resized
with the mouse they snap to the edges and middles of the others and of the panel - guides show it -
or to a raster of 8 pixels (Alt: freely); Shift or Ctrl add to the selection, a frame drawn on the
free surface selects what it touches, the arrow keys move the selection, *Arrange* lines several
up. The inspector binds them to ports of the flow and places them exactly; undo/redo for every
change. *Operate*: the widgets work – controls send their values into the running flow, displays
show the values of its outputs; the panel scales with its window as a whole; ``F11`` shows it full
screen as an application of its own (kiosk). Files: ``*.panel.yaml``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDrag,
    QKeySequence,
    QPainter,
    QPen,
    QPolygon,
    QShortcut,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import signals
from ...core.hub import Hub
from ...lab import panel_model
from ...lab.flow_files import load_flow
from ...lab.model import Flow, FlowError
from ...lab.panel_model import (
    CONTROL_KINDS,
    GRID,
    MIN_SIZE,
    WIDGET_KINDS,
    WIDGET_SIZES,
    Panel,
    PanelError,
    PanelWidget,
    Rect,
)
from .. import messages
from ..flow.runner import FlowRunner
from ..icons import icon, set_icon
from ..panel.widgets import PanelItem, make_item
from ..theme import ACCENT, BORDER, BORDER_STRONG, ERROR, PANEL, PANEL_LIGHT, TEXT_MUTED, set_role
from .base import DocumentWidget

log = logging.getLogger(__name__)

VIEWS = ("Edit", "Operate")
PANEL_FILE_FILTER = "Panels (*.panel.yaml);;All files (*)"


MERGE_SETTING = 1


class PanelEdit(QUndoCommand):
    """A change of the panel: the whole panel before and after (panels are small). Steps of one
    setting in a row (a spin box stepped up) are one undo step."""

    def __init__(self, document: "PanelDocument", text: str, before: dict, after: dict,
                 merge_key: Optional[tuple] = None) -> None:
        super().__init__(text)
        self.document, self.before, self.after = document, before, after
        self.merge_key = merge_key
        self._pushed = False

    def id(self) -> int:  # noqa: A003 - Qt naming
        return MERGE_SETTING if self.merge_key is not None else -1

    def mergeWith(self, other) -> bool:  # noqa: N802 - Qt naming
        if not isinstance(other, PanelEdit) or other.merge_key != self.merge_key:
            return False
        self.after = other.after
        return True

    def undo(self) -> None:
        self.document._inspector = None  # shows the restored settings
        self.document._restore(self.before)

    def redo(self) -> None:
        if self._pushed:
            self.document._inspector = None
        self._pushed = True
        self.document._restore(self.after)


#: the smallest panel (pixels)
MIN_PANEL = (200, 120)
#: the signal types each kind of widget can show or set (missing: any)
KIND_TYPES = {
    "number": (signals.SCALAR, signals.ANALOG, signals.BOOL),
    "led": (signals.BOOL, signals.SCALAR, signals.DIGITAL),
    "chart": (signals.SCALAR, signals.ANALOG, signals.TABLE),
    "scope": (signals.CAPTURE, signals.DIGITAL, signals.ANALOG),
    "switch": (signals.BOOL, signals.SCALAR),
    "button": (signals.EVENT, signals.BOOL, signals.SCALAR),
    "slider": (signals.SCALAR,),
    "input": (signals.SCALAR,),
    "choice": (signals.SCALAR,),
}


class _Palette(QListWidget):
    """The widget kinds; dragged onto the grid they land where they are dropped."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)

    def startDrag(self, actions) -> None:  # noqa: N802 - Qt naming
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData(PANEL_WIDGET_MIME, str(item.data(Qt.UserRole)).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class _Selector(QObject):
    """In the editor the mouse acts on the surface of the panel, not on the widget under it: a click
    selects the widget, a drag moves it (:class:`_EditPage`)."""

    def __init__(self, page: "_EditPage", widget_id: str) -> None:
        super().__init__(page)
        self.page, self.widget_id = page, widget_id

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        kind = event.type()
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.MouseMove, QEvent.MouseButtonRelease):
            point = self.page.mapFromGlobal(event.globalPosition().toPoint())
            if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.page.press(point, self.widget_id, event.modifiers())
            elif kind == QEvent.MouseMove:
                self.page.drag_move(point, event.buttons(), event.modifiers())
            elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self.page.release()
            return True
        if kind == QEvent.ContextMenu:
            self.page.context_menu(event.globalPos(), self.widget_id)
            return True
        if kind == QEvent.Wheel:
            # not into the widget (a spin box would change), but to the editor so it scrolls
            area = self.page.document.edit_area
            for bar, delta in ((area.verticalScrollBar(), event.pixelDelta().y() or event.angleDelta().y() / 4),
                               (area.horizontalScrollBar(), event.pixelDelta().x() or event.angleDelta().x() / 4)):
                bar.setValue(int(bar.value() - delta))
            return True
        return False


#: the room around the panel in the editor (pixels)
ORIGIN = 24
#: the size of a handle of a selected widget (pixels)
HANDLE = 8
#: how close an edge comes to another before it snaps (pixels)
SNAP_DISTANCE = 6
#: a panel that is operated is drawn at least this large (its scale); a smaller window scrolls
MIN_SCALE = 0.5
#: the room of a group's frame around its widgets, and above them for its title (pixels)
GROUP_PADDING = 8
GROUP_TITLE = 22
#: the edges each handle of a selected widget moves, and its cursor
CURSORS = {
    "left top": Qt.SizeFDiagCursor, "top": Qt.SizeVerCursor, "right top": Qt.SizeBDiagCursor,
    "right": Qt.SizeHorCursor, "right bottom": Qt.SizeFDiagCursor, "bottom": Qt.SizeVerCursor,
    "left bottom": Qt.SizeBDiagCursor, "left": Qt.SizeHorCursor,
}
PANEL_WIDGET_MIME = "application/x-openscilab-panel-widget"


def _handles(rect: QRect) -> dict[str, QPoint]:
    """The handles of a widget's rectangle: where each one sits."""
    left, top, right, bottom = rect.left(), rect.top(), rect.left() + rect.width(), rect.top() + rect.height()
    middle_x, middle_y = (left + right) // 2, (top + bottom) // 2
    return {"left top": QPoint(left, top), "top": QPoint(middle_x, top), "right top": QPoint(right, top),
            "right": QPoint(right, middle_y), "right bottom": QPoint(right, bottom),
            "bottom": QPoint(middle_x, bottom), "left bottom": QPoint(left, bottom), "left": QPoint(left, middle_y)}


def _union(rects: Iterable[Rect]) -> Rect:
    rects = list(rects)
    left, top = min(rect[0] for rect in rects), min(rect[1] for rect in rects)
    right = max(rect[0] + rect[2] for rect in rects)
    bottom = max(rect[1] + rect[3] for rect in rects)
    return left, top, right - left, bottom - top


def _paint_groups(painter: QPainter, widgets: list[PanelWidget], to_page: Callable[[PanelWidget], QRect]) -> None:
    """A frame with its title around the widgets of each group."""
    groups: dict[str, QRect] = {}
    for widget in widgets:
        if widget.group:
            rect = to_page(widget)
            groups[widget.group] = groups[widget.group].united(rect) if widget.group in groups else rect
    painter.setBrush(Qt.NoBrush)
    for name, rect in groups.items():
        frame = rect.adjusted(-GROUP_PADDING, -GROUP_TITLE, GROUP_PADDING, GROUP_PADDING)
        painter.setPen(QPen(QColor(BORDER_STRONG), 1))
        painter.drawRoundedRect(frame, 6, 6)
        painter.setPen(QColor(TEXT_MUTED))
        painter.drawText(frame.adjusted(10, 3, -10, 0), Qt.AlignLeft | Qt.AlignTop, name)


class _Overlay(QWidget):
    """Over the widgets of the editor: the selection with its handles, the corner of the panel, the
    guides of snapping, the frame that selects and where a widget from the palette lands."""

    def __init__(self, page: "_EditPage") -> None:
        super().__init__(page)
        self.page = page
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        page = self.page
        painter = QPainter(self)
        accent = QColor(ACCENT)
        selected = page.selected_here()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(accent, 2))
        for widget_id in selected:
            painter.drawRect(page.page_rect(page.rect_of(widget_id)).adjusted(-1, -1, 1, 1))
        if len(selected) == 1:
            painter.setPen(QPen(accent, 1))
            painter.setBrush(QColor(PANEL_LIGHT))
            for point in _handles(page.page_rect(page.rect_of(selected[0]))).values():
                painter.drawRect(QRect(point.x() - HANDLE // 2, point.y() - HANDLE // 2, HANDLE, HANDLE))
        width, height = page.panel_size()
        corner = QPoint(ORIGIN + width, ORIGIN + height)
        painter.setPen(QPen(QColor(TEXT_MUTED), 1.2))
        for step in (4, 8, 12):  # the corner of the panel: drag it to resize the panel
            painter.drawLine(corner.x() - step, corner.y() - 2, corner.x() - 2, corner.y() - step)
        painter.setPen(QPen(QColor(ERROR), 1))
        for axis, position, low, high in page.guides:
            if axis == "x":
                painter.drawLine(QPointF(ORIGIN + position, ORIGIN + low - 8), QPointF(ORIGIN + position, ORIGIN + high + 8))
            else:
                painter.drawLine(QPointF(ORIGIN + low - 8, ORIGIN + position), QPointF(ORIGIN + high + 8, ORIGIN + position))
        for rect in (page.band, page.ghost):
            if rect is not None:
                fill = QColor(accent)
                fill.setAlpha(36)
                painter.setBrush(fill)
                painter.setPen(QPen(accent, 1, Qt.DashLine))
                painter.drawRect(rect.adjusted(0, 0, -1, -1))


class _EditPage(QWidget):
    """A tab of the panel in the editor: the surface of the panel with its raster and the widgets on
    it. They are selected (Shift or Ctrl add, a frame drawn on the free surface takes what it
    touches), moved and resized with the mouse - snapping to the others, to the panel and to the
    raster (:func:`~openscilab.lab.panel_model.snap`; Alt places them freely) - and moved with the
    arrow keys (Shift: by the raster); the corner of the panel resizes it."""

    def __init__(self, document: "PanelDocument", tab: str) -> None:
        super().__init__()
        self.document, self.tab = document, tab
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.items: dict[str, PanelItem] = {}
        #: places while the mouse moves or resizes widgets (pixels of the panel), until they are kept
        self.live: dict[str, Rect] = {}
        #: the size of the panel while its corner is dragged
        self.live_size: Optional[tuple[int, int]] = None
        #: the guides of the snap the mouse made: see panel_model.guides_for
        self.guides: list = []
        #: the frame that selects, and where a widget from the palette lands (pixels of the page)
        self.band: Optional[QRect] = None
        self.ghost: Optional[QRect] = None
        self._drag: Optional[dict] = None
        self._cursor = Qt.ArrowCursor
        self.overlay = _Overlay(self)
        self._fit_panel()

    # ----------------------------------------------------------- geometry
    def panel_size(self) -> tuple[int, int]:
        panel = self.document.panel
        return self.live_size or (panel.width, panel.height)

    def _fit_panel(self) -> None:
        width, height = self.panel_size()
        self.setMinimumSize(width + 2 * ORIGIN, height + 2 * ORIGIN)

    def widgets(self) -> list[PanelWidget]:
        return [widget for widget in self.document.panel.widgets if widget.tab == self.tab]

    def rect_of(self, widget_id: str) -> Rect:
        return self.live.get(widget_id) or self.document.panel.widget(widget_id).rect

    @staticmethod
    def page_rect(rect: Rect) -> QRect:
        x, y, width, height = rect
        return QRect(ORIGIN + x, ORIGIN + y, width, height)

    @staticmethod
    def to_panel(point: QPoint) -> QPoint:
        return QPoint(point.x() - ORIGIN, point.y() - ORIGIN)

    def selected_here(self) -> list[str]:
        return [widget_id for widget_id in self.document.selection if widget_id in self.items]

    def widget_at(self, point: QPoint) -> Optional[str]:
        """The widget on top at ``point`` (pixels of the page)."""
        for widget in reversed(self.widgets()):
            if self.page_rect(self.rect_of(widget.id)).contains(point):
                return widget.id
        return None

    def add_item(self, item: PanelItem) -> None:
        """A widget of the panel on this page: the mouse and the keys act on the page, not on it."""
        self.items[item.model.id] = item
        selector = _Selector(self, item.model.id)
        for child in [item, *item.findChildren(QWidget)]:
            child.installEventFilter(selector)
            child.setMouseTracking(True)
            child.setFocusPolicy(Qt.NoFocus)

    def place_items(self) -> None:
        for widget_id, item in self.items.items():
            item.setGeometry(self.page_rect(self.rect_of(widget_id)))
            warning = item.findChild(QLabel, "binding-warning")
            if warning is not None:
                warning.move(item.width() - 22, 4)
        for widget_id in self.live:
            self.items[widget_id].raise_()  # (what the mouse drags lies on the others while it moves)
        self.overlay.setGeometry(self.rect())
        self.overlay.raise_()
        self.overlay.update()
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.overlay.setGeometry(self.rect())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        width, height = self.panel_size()
        surface = QRect(ORIGIN, ORIGIN, width, height)
        painter.fillRect(surface, QColor(PANEL))
        painter.setPen(QPen(QColor(BORDER_STRONG), 1))
        step = GRID * 2  # the raster: a dot every second step
        painter.drawPoints(QPolygon([QPoint(ORIGIN + x, ORIGIN + y) for x in range(step, width, step)
                                     for y in range(step, height, step)]))
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawRect(surface.adjusted(0, 0, -1, -1))
        _paint_groups(painter, self.widgets(), lambda widget: self.page_rect(self.rect_of(widget.id)))

    # -------------------------------------------------------------- mouse
    def hit(self, point: QPoint) -> Optional[tuple[str, str]]:
        """The handle at ``point``: ``("resize", sides)`` of the widget selected alone, ``("panel",
        "right bottom")`` the corner of the panel; ``None``."""
        selected = self.selected_here()
        if len(selected) == 1:
            for sides, handle in _handles(self.page_rect(self.rect_of(selected[0]))).items():
                if abs(point.x() - handle.x()) <= HANDLE and abs(point.y() - handle.y()) <= HANDLE:
                    return "resize", sides
        width, height = self.panel_size()
        if abs(point.x() - ORIGIN - width) <= HANDLE and abs(point.y() - ORIGIN - height) <= HANDLE:
            return "panel", "right bottom"
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.press(event.position().toPoint(), None, event.modifiers())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.drag_move(event.position().toPoint(), event.buttons(), event.modifiers())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.release()

    def press(self, point: QPoint, widget_id: Optional[str], modifiers) -> None:
        """The left button went down at ``point`` (pixels of the page), on the widget ``widget_id``."""
        self.setFocus(Qt.MouseFocusReason)
        document = self.document
        start = self.to_panel(point)
        grip = self.hit(point)
        if grip is not None:
            if grip[0] == "resize":
                selected = self.selected_here()[0]
                self._drag = {"kind": "resize", "ids": [selected], "sides": grip[1], "start": start,
                              "rect": self.rect_of(selected)}
            else:
                self._drag = {"kind": "panel", "start": start, "size": self.panel_size(), "ids": []}
            return
        additive = bool(modifiers & (Qt.ShiftModifier | Qt.ControlModifier | Qt.MetaModifier))
        if widget_id is not None:
            if additive:
                document.toggle_selection(widget_id)
                if widget_id not in document.selection:
                    return
            elif widget_id not in document.selection:
                document.select(widget_id)
            ids = self.selected_here()
            self._drag = {"kind": "move", "ids": ids, "start": start, "rects": {key: self.rect_of(key) for key in ids},
                          "moved": False, "clicked": None if additive else widget_id}
            return
        if not additive:
            document.select(None)
        before = list(document.selection)
        self._drag = {"kind": "band", "start": point, "before": before, "touched": before, "ids": []}

    def drag_move(self, point: QPoint, buttons, modifiers) -> None:
        """The mouse moved to ``point``: a drag goes on (with the left button), else the cursor shows
        what a press would take hold of."""
        drag = self._drag
        if drag is None or not buttons & Qt.LeftButton:
            self._hover(point)
            return
        free = bool(modifiers & Qt.AltModifier)
        threshold, grid = (0, 0) if free else (SNAP_DISTANCE, GRID)
        bounds = self.panel_size()
        position = self.to_panel(point)
        dx, dy = position.x() - drag["start"].x(), position.y() - drag["start"].y()
        others = [widget.rect for widget in self.widgets() if widget.id not in drag["ids"]]
        kind = drag["kind"]
        if kind == "move":
            if not drag["moved"] and abs(dx) + abs(dy) < 4:
                return  # (a click, not a drag yet)
            drag["moved"] = True
            union = _union(drag["rects"].values())
            snapped, guides = panel_model.snap((union[0] + dx, union[1] + dy, union[2], union[3]), others, bounds,
                                               "move", threshold, grid)
            shift_x, shift_y = snapped[0] - union[0], snapped[1] - union[1]
            self.live = {key: (x + shift_x, y + shift_y, width, height)
                         for key, (x, y, width, height) in drag["rects"].items()}
            self.guides = [] if free else guides
        elif kind == "resize":
            x, y, width, height = drag["rect"]
            sides = drag["sides"]
            if "left" in sides:
                x, width = x + dx, width - dx
            if "right" in sides:
                width += dx
            if "top" in sides:
                y, height = y + dy, height - dy
            if "bottom" in sides:
                height += dy
            snapped, guides = panel_model.snap((x, y, width, height), others, bounds, sides, threshold, grid)
            self.live = {drag["ids"][0]: snapped}
            self.guides = [] if free else guides
        elif kind == "panel":
            width, height = drag["size"]
            needed = self.document.panel_extent()
            step = grid or 1
            self.live_size = (max(round((width + dx) / step) * step, needed[0], MIN_PANEL[0]),
                              max(round((height + dy) / step) * step, needed[1], MIN_PANEL[1]))
            self._fit_panel()
        else:
            self.band = QRect(drag["start"], point).normalized()
            touched = drag["before"] + [widget.id for widget in self.widgets() if widget.id not in drag["before"]
                                        and self.page_rect(widget.rect).intersects(self.band)]
            if touched != drag["touched"]:
                drag["touched"] = touched
                self.document.select_many(touched)
        self.place_items()

    def release(self) -> None:
        """The left button came up: what was dragged is kept (one undo step)."""
        drag, self._drag = self._drag, None
        live, self.live = self.live, {}
        size, self.live_size = self.live_size, None
        self.guides, self.band = [], None
        document = self.document
        if drag is not None and drag["kind"] in ("move", "resize") and live \
                and any(rect != document.panel.widget(key).rect for key, rect in live.items()):
            document.set_places(live, "Move" if drag["kind"] == "move" else "Resize")  # (builds the pages again)
            return
        if drag is not None and drag["kind"] == "panel" and size is not None \
                and size != (document.panel.width, document.panel.height):
            document.set_panel_size(*size)
            return
        if drag is not None and drag["kind"] == "move" and not drag["moved"] and drag["clicked"] \
                and len(document.selection) > 1:
            document.select(drag["clicked"])  # (a click on one of several selected: that one alone)
        self._fit_panel()
        self.place_items()

    def _hover(self, point: QPoint) -> None:
        grip = self.hit(point)
        cursor = CURSORS[grip[1]] if grip is not None else \
            (Qt.SizeAllCursor if self.widget_at(point) is not None else Qt.ArrowCursor)
        if cursor != self._cursor:
            self._cursor = cursor
            for widget in [self, *self.findChildren(QWidget)]:
                widget.setCursor(cursor)

    # --------------------------------------------------------- keys, menu
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        document = self.document
        step = GRID if event.modifiers() & Qt.ShiftModifier else 1
        moves = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0), Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}
        if event.key() in moves and document.selection:
            document.move_selected(*moves[event.key()])
        elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and document.selection:
            document.remove_selected()
        elif event.matches(QKeySequence.SelectAll):
            document.select_many([widget.id for widget in self.widgets()])
        elif event.key() == Qt.Key_Escape and document.selection:
            document.select(None)
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.context_menu(event.globalPos(), self.widget_at(event.pos()))

    def context_menu(self, position: QPoint, widget_id: Optional[str]) -> None:
        if widget_id is not None and widget_id not in self.document.selection:
            self.document.select(widget_id)
        menu = QMenu(self)
        self.document.fill_arrange_menu(menu)
        menu.exec(position)

    # ------------------------------------------------- from the palette
    def _drop_place(self, point: QPoint, kind: str) -> Rect:
        """Where a widget of ``kind`` dropped at ``point`` lands (snapped like a moved one)."""
        width, height = WIDGET_SIZES[kind]
        corner = self.to_panel(point) - QPoint(16, 16)
        others = [widget.rect for widget in self.widgets()]
        place, self.guides = panel_model.snap((corner.x(), corner.y(), width, height), others, self.panel_size(),
                                              "move", SNAP_DISTANCE, GRID)
        return place

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasFormat(PANEL_WIDGET_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        kind = bytes(event.mimeData().data(PANEL_WIDGET_MIME)).decode("ascii")
        if kind not in WIDGET_SIZES:
            return
        self.ghost = self.page_rect(self._drop_place(event.position().toPoint(), kind))
        self.overlay.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.ghost, self.guides = None, []
        self.overlay.update()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        kind = bytes(event.mimeData().data(PANEL_WIDGET_MIME)).decode("ascii")
        self.ghost = None
        if kind not in WIDGET_SIZES:
            return
        x, y, _width, _height = self._drop_place(event.position().toPoint(), kind)
        self.guides = []
        self.overlay.update()
        event.acceptProposedAction()
        QTimer.singleShot(0, lambda: self.document.drop_new(kind, self.tab, (x, y)))


class _RunPage(QWidget):
    """A tab of the panel that is operated: the widgets where the editor put them, the panel as a
    whole scaled to the room it has (not below :data:`MIN_SCALE`: a smaller window scrolls)."""

    def __init__(self, document: "PanelDocument", tab: str) -> None:
        super().__init__()
        self.document, self.tab = document, tab
        self.items: dict[str, PanelItem] = {}
        panel = document.panel
        self.setMinimumSize(int(panel.width * MIN_SCALE), int(panel.height * MIN_SCALE))

    def add_item(self, item: PanelItem) -> None:
        self.items[item.model.id] = item

    def scale(self) -> float:
        panel = self.document.panel
        return max(min(self.width() / panel.width, self.height() / panel.height), MIN_SCALE)

    def page_rect(self, rect: Rect) -> QRect:
        """Where a place of the panel is on the page: scaled, the panel in the middle."""
        scale = self.scale()
        left = max((self.width() - self.document.panel.width * scale) / 2, 0)
        x, y, width, height = rect
        return QRect(round(left + x * scale), round(y * scale), round(width * scale), round(height * scale))

    def place_items(self) -> None:
        for item in self.items.values():
            item.setGeometry(self.page_rect(item.model.rect))
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.place_items()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        _paint_groups(painter, [item.model for item in self.items.values()],
                      lambda widget: self.page_rect(widget.rect))


class _Kiosk(QWidget):
    """The panel that is operated full screen, as a window of its own; F11 or Esc closes it.

    A child of the panel document (a window, but owned by Qt) that deletes itself when it is
    closed: its end is a moment Qt chooses, never one of Python's garbage collector - a window freed
    by the collector in the middle of something Qt does with its windows crashes the application."""

    #: closed: the document takes its content back
    closed = Signal()

    def __init__(self, parent: QWidget, title: str, content: QWidget) -> None:
        super().__init__(parent, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(title)
        self.content = content
        layout = QVBoxLayout(self)
        layout.addWidget(content)
        content.show()
        for key in (Qt.Key_F11, Qt.Key_Escape):
            QShortcut(QKeySequence(key), self, activated=self.close)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.closed.emit()
        super().closeEvent(event)


class PanelDocument(DocumentWidget):
    """A panel (see the module documentation)."""

    document_kind = "panel"
    runnable = True
    run_state_changed = Signal(str)
    execution_line = Signal(str)
    selection_changed = Signal()
    #: show the flow of the panel: its open document, or its file
    flow_requested = Signal(object)

    def __init__(self, panel: Optional[Panel] = None, hub: Optional[Hub] = None, path: Optional[str] = None,
                 flow: Optional[Flow] = None, project=None, parent: Optional[QWidget] = None,
                 flow_document=None) -> None:
        super().__init__(parent)
        self.panel = panel or Panel()
        self.hub = hub
        #: the open flow document the panel was made for (its flow may not be saved yet)
        self._flow_document = flow_document
        if project is None and flow_document is not None:
            project = flow_document.project
        self.project = project
        self._path = path
        self._flow = flow
        #: the selected widgets (ids), the last one selected last
        self.selection: list[str] = []
        self.items: dict[str, PanelItem] = {}
        self.kiosk: Optional[QWidget] = None
        self.undo = QUndoStack(self)
        # methods, not lambdas: Qt drops the connection when the document is gone (a subflow shares
        # its parent's stack, which outlives it)
        self.undo.cleanChanged.connect(self._undo_state_changed)
        self.undo.indexChanged.connect(self._undo_state_changed)  # undo/redo states
        self._keep_inspector = False
        self._flow_stamp: Optional[float] = None
        #: finds the open flow document of a file (set by the shell)
        self.flow_provider = None
        self.runner = FlowRunner(self)
        #: controls whose binding the running flow does not have (reported once per run)
        self._unbound: set[str] = set()
        self.runner.values.connect(self._on_values)
        self.runner.state_changed.connect(self._run_state)
        self.runner.log.connect(self.execution_line.emit)
        self.runner.finished.connect(lambda result: self.execution_line.emit(
            f"■ {result.state}" + (f": {result.error}" if result.error else "")))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        from ..widgets.toolbar import HIGH, LOW, NORMAL, AdaptiveToolBar

        self._toolbar = bar = AdaptiveToolBar("panel", "Panel", self)
        # Edit / Operate as two toggles side by side (as Graph / YAML in a flow)
        self.view_actions = []
        for view, icon_name, tip in (("Edit", "pencil", "Arrange and bind the widgets"),
                                     ("Operate", "play", "Use the panel as it runs")):
            action = QAction(icon(icon_name), view, self)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.triggered.connect(lambda _checked=False, view=view: self.set_view(view))
            bar.add_action(f"view-{view.lower()}", action, "view", HIGH)
            self.view_actions.append(action)
        self.view_actions[0].setChecked(True)
        self.action_run = QAction(icon("play"), "Run", self)
        self.action_run.triggered.connect(self.start_run)
        self.action_stop = QAction(icon("stop"), "Stop", self)
        self.action_stop.triggered.connect(self.stop_run)
        self.action_kiosk = QAction(icon("detach"), "Full screen (F11)", self)
        self.action_kiosk.setIconText("Full screen")
        self.action_kiosk.triggered.connect(self.toggle_kiosk)
        self.action_delete = QAction(icon("trash"), "Remove widget", self)
        self.action_delete.setShortcut(QKeySequence.Delete)
        self.action_delete.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.action_delete.triggered.connect(self.remove_selected)
        bar.add_action("run", self.action_run, "run", HIGH, pinned=True)
        bar.add_action("stop", self.action_stop, "run", HIGH, text=False)
        self.fast_box = QCheckBox("Virtual time", self)
        self.fast_box.setToolTip("Simulated instruments in the flow's own time: as fast as possible, the same "
                                 "result every run")
        bar.add_widget("virtual-time", self.fast_box, "Virtual time", "run", LOW)
        bar.add_action("full-screen", self.action_kiosk, "show", NORMAL, text=False)
        bar.add_action("remove", self.action_delete, "edit", LOW, text=False)
        self.addAction(self.action_delete)
        self.arrange_menu = QMenu(self)
        self.arrange_menu.aboutToShow.connect(lambda: self.fill_arrange_menu(self.arrange_menu))
        self.action_arrange = QAction(icon("align"), "Arrange", self)
        self.action_arrange.setToolTip("Line the selected widgets up, distribute them, bring them to the front")
        self.action_arrange.setMenu(self.arrange_menu)
        self.action_arrange.triggered.connect(lambda: self.arrange_menu.popup(QCursor.pos()))
        bar.add_action("arrange", self.action_arrange, "edit", NORMAL)
        self.action_open_flow = QAction(icon("nodes"), "Open flow", self)
        self.action_open_flow.setToolTip("Open the flow this panel works with")
        self.action_open_flow.triggered.connect(self.open_flow)
        bar.add_action("open-flow", self.action_open_flow, "flow", NORMAL)
        self.flow_label = QLabel(self)
        set_role(self.flow_label, "hint")
        bar.add_widget("flow", self.flow_label, "The flow", "flow", LOW)
        for action in (self.action_stop, self.action_kiosk, self.action_delete):
            action.setToolTip(action.toolTip() or action.text())
        bar.finish()
        layout.addWidget(self._toolbar)

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)
        # editor: palette and grid
        editor = QSplitter(Qt.Horizontal, self.stack)
        self.palette = _Palette(editor)
        for kind, (role, title) in WIDGET_KINDS.items():
            item = QListWidgetItem(f"{title}  ·  {role}", self.palette)
            item.setData(Qt.UserRole, kind)
        self.palette.setToolTip("Drag a widget onto the panel, or double-click to add it on a free place")
        self.palette.itemActivated.connect(lambda item: self.add_widget(item.data(Qt.UserRole)))
        self.palette.setMaximumWidth(220)
        self.edit_area = QScrollArea(editor)
        self.edit_area.setWidgetResizable(True)
        editor.addWidget(self.palette)
        editor.addWidget(self.edit_area)
        editor.setSizes([180, 800])
        self.stack.addWidget(editor)
        # operation
        self.operate_area = QScrollArea(self.stack)
        self.operate_area.setWidgetResizable(True)
        self.stack.addWidget(self.operate_area)

        QShortcut(QKeySequence(Qt.Key_F11), self, activated=self.toggle_kiosk, context=Qt.WidgetWithChildrenShortcut)
        self._inspector: Optional[QWidget] = None
        self.rebuild()
        self._update_run_actions()

    # ------------------------------------------------------------ identity
    def document_actions(self) -> list:
        """Run, stop, full screen, edit and operate, for the command palette."""
        return [self.action_run, self.action_stop, self.action_kiosk, self.action_delete, self.action_arrange,
                *self.view_actions]

    def _undo_state_changed(self, *_args) -> None:
        self.document_changed.emit()

    @property
    def title(self) -> str:
        if self._path:
            return os.path.basename(self._path)
        if self.panel.name and self.panel.name != "Panel":
            return self.panel.name
        if getattr(self, "_untitled", None) is None:
            from .base import untitled_title

            self._untitled = untitled_title("panel")
        return self._untitled

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def dirty(self) -> bool:
        return not self.undo.isClean()

    def undo_stack(self):
        return self.undo

    def toolbar(self):
        return None

    def views(self) -> list[str]:
        return list(VIEWS)

    def current_view(self) -> str:
        return VIEWS[self.stack.currentIndex()]

    def set_view(self, name: str) -> None:
        if name not in VIEWS:
            return
        self.stack.setCurrentIndex(VIEWS.index(name))
        for view, action in zip(VIEWS, self.view_actions):
            action.setChecked(view == name)
        self.rebuild()
        self.document_changed.emit()

    @property
    def operating(self) -> bool:
        return self.current_view() == "Operate"

    # ---------------------------------------------------------------- flow
    @property
    def flow_document(self):
        """The flow document the panel was made for, while it is open."""
        document = self._flow_document
        if document is not None:
            import shiboken6

            if not shiboken6.isValid(document):
                self._flow_document = document = None
        return document

    def flow_file(self) -> Optional[str]:
        path = panel_model.flow_path(self.panel, self._path)
        if not path and self.flow_document is not None and self.flow_document.path:
            return os.path.abspath(self.flow_document.path)  # the flow was saved after the panel was made
        return path

    def flow(self) -> Optional[Flow]:
        """The flow the panel works with: the open flow document of its file (with its unsaved
        changes), else the file, read again when it changed."""
        path = self.flow_file()
        if not path:
            return self.flow_document.flow if self.flow_document is not None else self._flow
        open_document = self.flow_provider(path) if self.flow_provider is not None else None
        if open_document is not None:
            return open_document.flow
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return self._flow
        if self._flow is None or stamp != self._flow_stamp:
            try:
                self._flow = load_flow(path, registry=self.registry())
                self._flow_stamp = stamp
            except (FlowError, OSError, SyntaxError) as error:
                self.execution_line.emit(f"{path}: {error}")
        return self._flow

    def registry(self):
        if self.flow_document is not None:
            return self.flow_document.registry
        return self.project.registry() if self.project is not None else None

    def open_flow(self) -> bool:
        """Show the flow of the panel (the flow document it was made for while that is open)."""
        target = self.flow_document or self.flow_file()
        if target is None:
            return False
        self.flow_requested.emit(target)
        return True

    def set_flow_file(self, path: str) -> None:
        """Bind the panel to the flow in ``path``."""
        before = self.panel.to_data()
        relative = path
        if self._path:
            relative = os.path.relpath(path, os.path.dirname(os.path.abspath(self._path)))
        self.panel.flow = relative
        self._flow = None
        self._flow_stamp = None
        self._flow_document = None
        self._push("Set the flow", before)

    def ports(self, role: str, kind: str = "") -> list[str]:
        """``node.port`` a widget of ``role`` can bind to (displays: outputs; controls: both), only
        those of a type the ``kind`` of widget shows (an LED a bool, a scope a capture, ...)."""
        flow = self.flow()
        if flow is None:
            return []
        result = []
        for node_id, node in flow.nodes.items():
            try:
                inputs, outputs = flow.spec(node, self.registry()).resolve_ports(node.params)
            except Exception as error:  # noqa: BLE001 - an unknown node type has no ports to offer
                log.debug("No ports of %s: %s", node_id, error)
                continue
            accepted = KIND_TYPES.get(kind)

            def fitting(port, accepted=accepted) -> bool:
                return accepted is None or port.type in accepted or port.type == signals.ANY

            if role == "control":
                result += [f"{node_id}.{port.name}" for port in inputs if fitting(port)]
            result += [f"{node_id}.{port.name}" for port in outputs if fitting(port)]
        return result

    # ------------------------------------------------------------- editing
    def _push(self, text: str, before: dict, merge_key: Optional[tuple] = None) -> None:
        after = self.panel.to_data()
        if after != before:
            if not self._keep_inspector:
                self._inspector = None  # (shows what changed; a change typed into it keeps it)
            self.undo.push(PanelEdit(self, text, before, after, merge_key))

    def _restore(self, data: dict) -> None:
        self.panel = Panel.from_data(data)
        known = {widget.id for widget in self.panel.widgets}
        self.selection = [widget_id for widget_id in self.selection if widget_id in known]
        self.rebuild()
        self.document_changed.emit()

    @property
    def selected(self) -> Optional[str]:
        """The widget selected alone (``None``: none, or several)."""
        return self.selection[0] if len(self.selection) == 1 else None

    def add_widget(self, kind: str, bind: str = "", **settings) -> PanelWidget:
        """A new widget (without ``x`` and ``y`` on a free place), selected."""
        before = self.panel.to_data()
        if not bind and kind != "label":
            ports = self.ports(WIDGET_KINDS[kind][0], kind)
            bind = ports[0] if len(ports) == 1 else ""
        widget = self.panel.add(kind, bind, **settings)
        self.selection = [widget.id]
        self._inspector = None  # (shows the new widget)
        self._push(f"Add {kind}", before)
        self.selection_changed.emit()
        return widget

    def update_widget(self, widget_id: str, **changes) -> None:
        """Change settings of a widget (``options`` replaces the options); the panel grows to hold it."""
        before = self.panel.to_data()
        widget = self.panel.widget(widget_id)
        for name, value in changes.items():
            setattr(widget, name, value)
        widget.x, widget.y = max(int(widget.x), 0), max(int(widget.y), 0)
        widget.width, widget.height = max(int(widget.width), MIN_SIZE[0]), max(int(widget.height), MIN_SIZE[1])
        self.panel.grow_to_fit()
        if widget.tab and widget.tab not in self.panel.tabs:
            self.panel.tabs.append(widget.tab)
        self._push(f"Change {widget_id}", before, merge_key=(widget_id, tuple(sorted(changes))))

    def _inspector_update(self, widget_id: str, **changes) -> None:
        """A change typed into the inspector: the inspector stays (with the cursor in it)."""
        self._keep_inspector = True
        try:
            self.update_widget(widget_id, **changes)
        finally:
            self._keep_inspector = False

    def set_places(self, places: dict[str, Rect], text: str = "Move") -> None:
        """New places of widgets - moved or resized with the mouse, lined up: one undo step."""
        before = self.panel.to_data()
        for widget_id, rect in places.items():
            self.panel.widget(widget_id).rect = rect
        self.panel.grow_to_fit()
        self._push(text, before)

    def move_selected(self, dx: int, dy: int) -> None:
        """Move the selected widgets by ``dx``, ``dy`` pixels within the panel (the arrow keys); moves
        in a row are one undo step."""
        if not self.selection or self.operating:
            return
        rects = {widget_id: self.panel.widget(widget_id).rect for widget_id in self.selection}
        x, y, width, height = _union(rects.values())
        dx = min(max(dx, -x), self.panel.width - x - width)
        dy = min(max(dy, -y), self.panel.height - y - height)
        if not dx and not dy:
            return
        before = self.panel.to_data()
        for widget_id, (left, top, w, h) in rects.items():
            self.panel.widget(widget_id).rect = (left + dx, top + dy, w, h)
        self._push("Move", before, merge_key=("move", tuple(self.selection)))

    def align_selected(self, how: str) -> None:
        """Line the selected widgets up (see :func:`~openscilab.lab.panel_model.align`)."""
        if len(self.selection) >= 2:
            self.set_places(panel_model.align({key: self.panel.widget(key).rect for key in self.selection}, how),
                            "Align")

    def distribute_selected(self, axis: str) -> None:
        """The same space between the selected widgets (see :func:`~openscilab.lab.panel_model.distribute`)."""
        if len(self.selection) >= 3:
            self.set_places(panel_model.distribute({key: self.panel.widget(key).rect for key in self.selection},
                                                   axis), "Distribute")

    def raise_selected(self, front: bool = True) -> None:
        """The selected widgets on top of the others (``front``) or below them."""
        if not self.selection:
            return
        before = self.panel.to_data()
        chosen = [widget for widget in self.panel.widgets if widget.id in self.selection]
        others = [widget for widget in self.panel.widgets if widget.id not in self.selection]
        self.panel.widgets = others + chosen if front else chosen + others
        self._push("Bring to front" if front else "Send to back", before)

    def panel_extent(self) -> tuple[int, int]:
        """The size the widgets of every tab need."""
        widgets = self.panel.widgets
        return (max((widget.x + widget.width for widget in widgets), default=0),
                max((widget.y + widget.height for widget in widgets), default=0))

    def set_panel_size(self, width: int, height: int, merge: bool = False) -> None:
        """The size of the panel (at least what its widgets need)."""
        before = self.panel.to_data()
        needed = self.panel_extent()
        self.panel.width = max(int(width), needed[0], MIN_PANEL[0])
        self.panel.height = max(int(height), needed[1], MIN_PANEL[1])
        self._push("Panel size", before, merge_key=("panel size",) if merge else None)

    def remove_selected(self) -> None:
        if not self.selection or self.operating:
            return
        before = self.panel.to_data()
        for widget_id in self.selection:
            self.panel.remove(widget_id)
        count, self.selection = len(self.selection), []
        self._push("Remove widget" if count == 1 else "Remove widgets", before)
        self.selection_changed.emit()

    def select(self, widget_id: Optional[str]) -> None:
        self.select_many([widget_id] if widget_id else [])

    def toggle_selection(self, widget_id: str) -> None:
        """Add ``widget_id`` to the selection, or take it out (Shift or Ctrl and a click)."""
        if widget_id in self.selection:
            self.select_many([key for key in self.selection if key != widget_id])
        else:
            self.select_many([*self.selection, widget_id])

    def select_many(self, widget_ids: Iterable[str]) -> None:
        self.selection = list(dict.fromkeys(widget_ids))
        for page in self._edit_pages():
            page.overlay.update()
        if not self._keep_inspector:
            self._inspector = None
        self.selection_changed.emit()
        self.document_changed.emit()

    def _edit_pages(self) -> list[_EditPage]:
        content = self.edit_area.widget()
        if isinstance(content, _EditPage):
            return [content]
        return content.findChildren(_EditPage) if content is not None else []

    def fill_arrange_menu(self, menu: QMenu) -> None:
        """*Arrange* (in the tool bar, on a right click in the editor): line up, distribute, order."""
        menu.clear()
        count = 0 if self.operating else len(self.selection)
        for how, title in (("left", "Align left edges"), ("center", "Align centres"),
                           ("right", "Align right edges"), ("top", "Align top edges"),
                           ("middle", "Align middles"), ("bottom", "Align bottom edges")):
            menu.addAction(title, lambda how=how: self.align_selected(how)).setEnabled(count >= 2)
            if how == "right":
                menu.addSeparator()
        menu.addSeparator()
        for axis, title in (("x", "Distribute horizontally"), ("y", "Distribute vertically")):
            menu.addAction(title, lambda axis=axis: self.distribute_selected(axis)).setEnabled(count >= 3)
        menu.addSeparator()
        menu.addAction("Bring to front", lambda: self.raise_selected(True)).setEnabled(count >= 1)
        menu.addAction("Send to back", lambda: self.raise_selected(False)).setEnabled(count >= 1)
        menu.addSeparator()
        menu.addAction("Select all", lambda: self.select_many(
            [widget.id for page in self._edit_pages() if page.isVisible() for widget in page.widgets()]))
        menu.addAction(self.action_delete)

    # ------------------------------------------------------------ building
    def rebuild(self) -> None:
        """Lay the widgets out again (in the editor or for operating): the same tab, scrolled as it was."""
        operating = self.operating
        area = self.operate_area if operating else self.edit_area
        old = area.widget()
        tab = old.tabText(old.currentIndex()) if isinstance(old, QTabWidget) else None
        focused = isinstance(QApplication.focusWidget(), _EditPage)
        scrolled = (area.horizontalScrollBar().value(), area.verticalScrollBar().value())
        self.items.clear()
        content = self._build_content(operating)
        old = area.takeWidget()
        area.setWidget(content)
        if old is not None:
            old.deleteLater()
        if isinstance(content, QTabWidget) and tab is not None:
            titles = [content.tabText(index) for index in range(content.count())]
            if tab in titles:
                content.setCurrentIndex(titles.index(tab))
        if scrolled != (0, 0):
            QTimer.singleShot(0, lambda: (area.horizontalScrollBar().setValue(scrolled[0]),
                                          area.verticalScrollBar().setValue(scrolled[1])))
        if focused and not operating:
            page = content.currentWidget() if isinstance(content, QTabWidget) else content
            page.setFocus(Qt.OtherFocusReason)
        flow = self.flow_file()
        problems = self.panel.problems(self.flow(), self.registry())
        self.flow_label.setText(("  Flow: " + os.path.basename(flow) if flow else "  No flow")
                                + (f"  ·  {len(problems)} problem(s)" if problems else ""))
        self.flow_label.setToolTip("\n".join(problems))
        self.action_open_flow.setEnabled(bool(flow) or self.flow_document is not None)
        self.action_delete.setEnabled(not operating)
        self.action_arrange.setEnabled(not operating)

    def _build_content(self, operating: bool) -> QWidget:
        tabs = self.panel.tab_names()
        pages = [self._build_page(tab, operating) for tab in tabs]
        if len(pages) == 1:
            return pages[0]
        widget = QTabWidget()
        for tab, page in zip(tabs, pages):
            widget.addTab(page, tab or "Main")
        return widget

    def _build_page(self, tab: str, operating: bool) -> QWidget:
        page = _RunPage(self, tab) if operating else _EditPage(self, tab)
        widgets = [widget for widget in self.panel.widgets if widget.tab == tab]
        for widget in widgets:
            page.add_item(self._item(widget, operating, page))
        if not widgets and not operating:
            hint = QLabel("Drag a widget from the palette onto the panel (or double-click it). Drag widgets to "
                          "move them, their handles to resize them: they snap to each other and to the panel "
                          "(Alt: freely). Shift adds to the selection, Arrange lines several up; the inspector "
                          "binds them to a port.", page)
            hint.setWordWrap(True)
            set_role(hint, "hint")
            hint.setAttribute(Qt.WA_TransparentForMouseEvents)
            hint.setGeometry(ORIGIN + 16, ORIGIN + 16, min(560, self.panel.width - 32), 80)
        page.place_items()
        return page

    def _item(self, widget: PanelWidget, operating: bool, parent: QWidget) -> PanelItem:
        item = make_item(widget, parent)
        self.items[widget.id] = item
        if operating:
            item.sent.connect(lambda value, model=widget: self.send(model, value))
            return item
        problem = self.binding_problem(widget)
        if problem:
            item.setToolTip(problem)
            warning = QLabel("⚠", item)
            warning.setObjectName("binding-warning")
            set_role(warning, "warning")
            warning.setToolTip(problem)
            warning.show()
        return item

    def drop_new(self, kind: str, tab: str, place: tuple[int, int]) -> PanelWidget:
        """A widget dragged from the palette, its top left corner at ``place`` on ``tab``."""
        return self.add_widget(kind, x=int(place[0]), y=int(place[1]), tab=tab)

    def binding_problem(self, widget: PanelWidget) -> str:
        """Why the widget's port does not work (empty when it does)."""
        if widget.role == "static":
            return ""
        if not widget.bind:
            return "Not bound to a port of the flow yet (Port in the inspector)"
        if self.flow() is not None and widget.bind not in self.ports(widget.role):
            return f"The flow has no port {widget.bind}"
        return ""

    # ----------------------------------------------------------- operating
    def send(self, widget: PanelWidget, value: Any) -> bool:
        """A control changed: its value goes into the running flow."""
        engine = self.runner.engine
        if not widget.bind or engine is None or not self.runner.running:
            return False
        try:
            engine.inject(widget.node, widget.port, panel_model.control_value(widget, value, engine.clock.now()))
        except FlowError as error:
            # bound to a node the flow no longer has: said once in the log, the run goes on
            if widget.id not in self._unbound:
                self._unbound.add(widget.id)
                self.execution_line.emit(f"Panel: {widget.label or widget.id}: {error}")
            return False
        return True

    def _on_values(self, node: str, port: str, values: list) -> None:
        """The values an output sent since the last delivery: every one of them reaches the
        displays bound to it (a chart adds a point for each)."""
        binding = f"{node}.{port}"
        for widget in self.panel.widgets:
            if widget.role == "display" and widget.bind == binding:
                item = self.items.get(widget.id)
                if item is not None:
                    for value in values:
                        item.show_value(value)

    @property
    def run_state(self) -> str:
        return self.runner.state if self.runner.running else "idle"

    def start_run(self) -> bool:
        if self.runner.running:
            return True
        flow = self.flow()
        if flow is None:
            messages.warning(self, "Run", "The panel has no flow.", "Choose its flow in the inspector.")
            return False
        problems = self.panel.problems(flow, self.registry())
        if problems:
            self.execution_line.emit("Panel: " + "; ".join(problems))
        if not self.operating:
            self.set_view("Operate")
        for item in self.items.values():
            item.reset()
        project = self.project
        flow = project.complete(flow.copy()) if project is not None else flow.copy()
        file = self.flow_file() or self._path
        if file:
            base = os.path.dirname(file)
        else:  # an unsaved flow writes where its document writes (a scratch folder)
            base = self.flow_document.data_dir if self.flow_document is not None else os.getcwd()
        self._unbound.clear()
        try:
            self.runner.start(flow, registry=self.registry(), mode="virtual" if self.fast_box.isChecked() else "real",
                              hub=self.hub, project=project,
                              base_dir=project.root if project else base,
                              data_dir=project.data_dir if project else base,
                              interactive=True, bound=self.panel.bindings())
        except FlowError as error:
            messages.warning(self, "Run", "The flow cannot run.", str(error))
            return False
        # the controls' settings go in once at the start
        for widget in self.panel.widgets:
            item = self.items.get(widget.id)
            if widget.kind in CONTROL_KINDS and widget.kind != "button" and item is not None \
                    and item.current() is not None and widget.bind:
                self.send(widget, item.current())
        self.execution_line.emit(f"▶ {self.panel.name}: {flow.name}")
        self._update_run_actions()
        self.run_state_changed.emit("running")
        return True

    def stop_run(self) -> None:
        self.runner.stop()

    def pause_run(self) -> None:
        self.runner.pause()

    def step_run(self) -> None:
        self.runner.step()

    def _run_state(self, state: str, message: str) -> None:
        self._update_run_actions()
        self.run_state_changed.emit(state)

    def _update_run_actions(self) -> None:
        running = self.runner.running and self.runner.state not in ("finished", "stopped", "error")
        self.action_run.setEnabled(not running)
        self.action_stop.setEnabled(running)

    def toggle_kiosk(self) -> None:
        """The operating panel full screen, as an application of its own (F11 or Esc back)."""
        if self.kiosk is not None:
            self.kiosk.close()
            return
        if not self.operating:
            self.set_view("Operate")
        window = _Kiosk(self, self.panel.name, self.operate_area.takeWidget())
        window.closed.connect(self._kiosk_closed)
        self.kiosk = window
        window.showFullScreen()

    def _kiosk_closed(self) -> None:
        """The full screen window closes: its content goes back into the document."""
        window, self.kiosk = self.kiosk, None
        if window is None:
            return
        content = window.content
        content.setParent(None)
        self.operate_area.setWidget(content)

    # ----------------------------------------------------------- inspector
    def inspector_widget(self, selection=None) -> Optional[QWidget]:
        if self._inspector is not None:
            return self._inspector
        holder = QWidget(self)
        holder.hide()
        form = QFormLayout(holder)
        form.setContentsMargins(10, 6, 10, 6)
        if len(self.selection) > 1:
            form.addRow("Widgets", QLabel(f"<b>{len(self.selection)}</b> selected", holder))
            note = QLabel("Arrange (in the tool bar, or on a right click) lines them up and distributes them; the "
                          "arrow keys move them (Shift: by the raster).", holder)
            note.setWordWrap(True)
            set_role(note, "hint")
            form.addRow(note)
        elif self.selected is None or self.selected not in {widget.id for widget in self.panel.widgets}:
            name = QLineEdit(self.panel.name, holder)
            name.editingFinished.connect(lambda: self._set_panel_name(name.text()))
            form.addRow("Panel", name)
            unsaved = self.flow_document is not None and not self.flow_file()
            flow = QLabel(f"{self.flow_document.title} (not saved yet)" if unsaved else self.panel.flow or "–", holder)
            flow.setWordWrap(True)
            form.addRow("Flow", flow)
            row = QHBoxLayout()
            browse = QPushButton(holder)
            set_icon(browse, "folder")
            browse.setToolTip("Choose another flow for this panel...")
            browse.clicked.connect(self._choose_flow)
            row.addWidget(browse)
            show = QPushButton("Open flow", holder)
            set_icon(show, "nodes")
            show.setToolTip("Open the flow this panel works with")
            show.setEnabled(bool(self.flow_file()) or self.flow_document is not None)
            show.clicked.connect(self.open_flow)
            row.addWidget(show)
            row.addStretch(1)
            form.addRow("", row)
            for name, title in (("width", "Width"), ("height", "Height")):
                spin = QSpinBox(holder)
                spin.setRange(MIN_PANEL[0 if name == "width" else 1], 20000)
                spin.setSingleStep(GRID)
                spin.setSuffix(" px")
                spin.setObjectName(f"setting-{name}")
                spin.setValue(getattr(self.panel, name))
                spin.setToolTip("The size of the panel (at least what its widgets need); a panel that is operated "
                                "grows or shrinks with its window")
                spin.valueChanged.connect(lambda value, key=name: self._set_panel_dimension(key, value))
                form.addRow(title, spin)
        else:
            widget = self.panel.widget(self.selected)
            form.addRow("Widget", QLabel(f"<b>{widget.id}</b> · {WIDGET_KINDS[widget.kind][1]}", holder))
            title = QLineEdit(widget.title, holder)
            title.editingFinished.connect(lambda: self._inspector_update(widget.id, title=title.text()))
            form.addRow("Title", title)
            if widget.role != "static":
                bind = QComboBox(holder)
                bind.setEditable(True)
                bind.addItems(self.ports(widget.role, widget.kind))
                bind.setCurrentText(widget.bind)
                bind.lineEdit().editingFinished.connect(lambda: self._inspector_update(widget.id, bind=bind.currentText()))
                bind.activated.connect(lambda _index: self._inspector_update(widget.id, bind=bind.currentText()))
                form.addRow("Port", bind)
            for name, title, low in (("x", "X", 0), ("y", "Y", 0), ("width", "Width", MIN_SIZE[0]),
                                     ("height", "Height", MIN_SIZE[1])):
                spin = QSpinBox(holder)
                spin.setRange(low, 20000)
                spin.setSuffix(" px")
                spin.setObjectName(f"setting-{name}")
                spin.setValue(getattr(widget, name))
                spin.valueChanged.connect(lambda value, key=name: self._inspector_update(widget.id, **{key: value}))
                form.addRow(title, spin)
            for name in ("tab", "group"):
                choices = (self.panel.tab_names() if name == "tab" else
                           sorted({item.group for item in self.panel.widgets if item.group}))
                box = QComboBox(holder)
                box.setEditable(True)  # a new name makes a new tab or group
                box.addItem("" if name == "group" else "")
                for choice in choices:
                    if choice:
                        box.addItem(choice)
                box.setCurrentText(getattr(widget, name))
                box.lineEdit().setPlaceholderText("Main" if name == "tab" else "none")
                box.setToolTip(f"Choose a {name}, or type a new name")
                box.setObjectName(f"setting-{name}")
                box.activated.connect(lambda _index, key=name, box=box: self._inspector_update(
                    widget.id, **{key: box.currentText().strip()}))
                box.lineEdit().editingFinished.connect(lambda key=name, box=box: self._inspector_update(
                    widget.id, **{key: box.currentText().strip()}))
                form.addRow(name.capitalize(), box)
            options = panel_model.OPTIONS.get(widget.kind, ())
            if options:
                text = QLineEdit(", ".join(f"{key}: {value}" for key, value in widget.options.items()), holder)
                text.setPlaceholderText(", ".join(f"{key}: …" for key in options))
                text.editingFinished.connect(lambda box=text: self._set_options(widget.id, box.text()))
                form.addRow("Options", text)
        self._inspector = holder
        return holder

    def _set_panel_name(self, name: str) -> None:
        before = self.panel.to_data()
        self.panel.name = name.strip() or self.panel.name
        self._push("Rename the panel", before)

    def _set_panel_dimension(self, name: str, value: int) -> None:
        """The width or height typed into the inspector: the inspector stays."""
        self._keep_inspector = True
        try:
            width, height = (value, self.panel.height) if name == "width" else (self.panel.width, value)
            self.set_panel_size(width, height, merge=True)
        finally:
            self._keep_inspector = False

    def _set_options(self, widget_id: str, text: str) -> None:
        import yaml

        try:
            options = yaml.safe_load("{" + text + "}") if text.strip() else {}
            PanelWidget.from_data(widget_id, {"kind": self.panel.widget(widget_id).kind, **options})
        except (yaml.YAMLError, PanelError) as error:
            messages.warning(self, "Options", "The options are not valid.", str(error))
            return
        self._inspector_update(widget_id, options=dict(options))

    def _choose_flow(self) -> None:
        start = os.path.dirname(self._path) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Flow of the panel", start, "Flows (*.flow.yaml *.py)")
        if path:
            self.set_flow_file(path)

    # --------------------------------------------------------------- files
    def can_save(self) -> bool:
        return True

    def save(self) -> bool:
        if not self._path:
            return self.save_as()
        return self._write(self._path)

    def save_as(self) -> bool:
        start = self._path or os.path.join(self.project.root, "panels", f"{self.panel.name}.panel.yaml") \
            if self.project is not None else (self._path or f"{self.panel.name}.panel.yaml")
        path, _ = QFileDialog.getSaveFileName(self, "Save panel", start, PANEL_FILE_FILTER)
        if not path:
            return False
        if not path.endswith(".panel.yaml"):
            path += ".panel.yaml"
        return self._write(path)

    def _write(self, path: str) -> bool:
        flow = self.flow_file()
        if not flow and self.flow_document is not None:
            # the panel keeps the file of its flow: the flow is saved first
            choice = messages.choose(self, "Save panel", "The flow of the panel is not saved yet.",
                                     ["Save the flow"],
                                     "The panel file names the file of its flow; save the flow to give it one.")
            if choice != 0 or not self.flow_document.save():
                return False
            flow = self.flow_file()
        if flow and not self.panel.flow:
            self.panel.flow = os.path.abspath(flow)
        if flow and self._path != path:
            # the flow stays the same file: relative to the new place of the panel
            self.panel.flow = os.path.relpath(flow, os.path.dirname(os.path.abspath(path)))
        try:
            panel_model.save(self.panel, path)
        except OSError as error:
            messages.error(self, "Save panel", f"{os.path.basename(path)} could not be written.", str(error))
            return False
        self._path = path
        self.undo.setClean()
        self.document_changed.emit()
        return True

    def shutdown(self) -> None:
        if self.kiosk is not None:
            self.kiosk.close()
        running = self.runner.running
        self.runner.detach()
        if running:
            self.runner.stop()
            self.runner.wait(5)


def open_panel_file(path: str, hub: Optional[Hub] = None) -> PanelDocument:
    """A panel document of a ``*.panel.yaml`` file (raises ``PanelError``/``OSError``)."""
    from ...lab.project import Project

    root = Project.find(path)
    project = Project.open(root) if root else None
    return PanelDocument(panel_model.load(path), hub=hub, path=path, project=project)
