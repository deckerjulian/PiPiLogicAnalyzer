# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Syntax colours for the YAML and Python views of a flow."""

from __future__ import annotations

import keyword
import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from ..theme import token


def _format(color: str, bold: bool = False) -> QTextCharFormat:
    result = QTextCharFormat()
    result.setForeground(QColor(color))
    if bold:
        result.setFontWeight(QFont.Bold)
    return result


class YamlHighlighter(QSyntaxHighlighter):
    RULES = [
        (re.compile(r"^\s*-?\s*([A-Za-z_][\w./-]*)\s*:(?=\s|$)"), _format(token("code.key"), True), 1),
        (re.compile(r"(?<=[{,]\s)([A-Za-z_][\w./-]*)(?=:\s)"), _format(token("code.key")), 1),
        (re.compile(r"(?<={)([A-Za-z_][\w./-]*)(?=:\s)"), _format(token("code.key")), 1),
        (re.compile(r"\b(\d+(\.\d+)?)(\s?(p|n|u|µ|m|k|M|G)?(s|Hz|V|A|%))?\b"), _format(token("code.number")), 0),
        (re.compile(r"'[^']*'|\"[^\"]*\""), _format(token("code.string")), 0),
        (re.compile(r"->"), _format(token("code.symbol"), True), 0),
        (re.compile(r"#.*$"), _format(token("code.comment")), 0),
    ]

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt naming
        for pattern, style, group in self.RULES:
            for match in pattern.finditer(text):
                start, end = match.span(group)
                self.setFormat(start, end - start, style)


class PythonHighlighter(QSyntaxHighlighter):
    KEYWORDS = re.compile(r"\b(" + "|".join(keyword.kwlist) + r")\b")
    RULES = [
        (KEYWORDS, _format(token("code.symbol"), True)),
        (re.compile(r"\b\d+(\.\d+)?\b"), _format(token("code.number"))),
        (re.compile(r"'[^']*'|\"[^\"]*\""), _format(token("code.string"))),
        (re.compile(r">>"), _format(token("code.key"), True)),
        (re.compile(r"#.*$"), _format(token("code.comment"))),
    ]

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt naming
        for pattern, style in self.RULES:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), style)
