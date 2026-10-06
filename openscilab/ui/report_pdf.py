# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reports as PDF: Qt lays the HTML of a report out on pages (also without a window)."""

from __future__ import annotations

import os


def write_pdf(text: str, path: str) -> None:
    """Write the HTML ``text`` as a PDF to ``path`` (A4)."""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        # a full application (not only QGuiApplication): windows may still be opened in this process
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from .. import qt_plugins

        qt_plugins.ensure_loadable_plugins()  # (as the application does: plugins hidden by iCloud)
        write_pdf.application = QApplication([])  # kept alive with the function
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    writer = QPdfWriter(path)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(15, 15, 15, 15),
                                     QPageLayout.Millimeter))
    writer.setResolution(150)
    document = QTextDocument()
    document.setHtml(embed_diagrams(document, text))
    document.print_(writer)


#: pixels per SVG unit of a diagram in the PDF
DIAGRAM_SCALE = 2


def embed_diagrams(document, text: str) -> str:
    """``text`` with every inline ``<svg>`` replaced by an image of it.

    Rich text of Qt knows no inline SVG (the diagrams of a report would be missing in the PDF),
    so each one is drawn into an image that the document holds as a resource.
    """
    import re

    from PySide6.QtCore import QByteArray, QUrl
    from PySide6.QtGui import QImage, QPainter, QTextDocument
    from PySide6.QtSvg import QSvgRenderer

    count = 0

    def image(match: "re.Match[str]") -> str:
        nonlocal count
        renderer = QSvgRenderer(QByteArray(match.group(0).encode("utf-8")))
        size = renderer.defaultSize()
        if not renderer.isValid() or size.isEmpty():
            return ""
        picture = QImage(size * DIAGRAM_SCALE, QImage.Format_ARGB32)
        picture.fill(0xFFFFFFFF)
        painter = QPainter(picture)
        renderer.render(painter)
        painter.end()
        count += 1
        name = f"diagram-{count}.png"
        document.addResource(QTextDocument.ImageResource, QUrl(name), picture)
        return f"<img src='{name}' width='{size.width()}' height='{size.height()}'/>"

    return re.sub(r"<svg\b.*?</svg>", image, text, flags=re.S)
