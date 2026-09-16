# Copyright (C) 2026 Julian Decker
# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
#
# Part of PiPiLogicAnalyzer, a port and extension of his software;
# the changes are described in docs/improvements.md and CHANGELOG.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Signal Description Language (SDL) parser.

Port of ``SignalDescriptionLanguage/Parser.cs``.  SDL describes a digital signal
as a list of ``;`` separated tokens::

    $0;                       initial level (0 or 1)
    h10;                      10 samples high
    l0x20;                    32 samples low
    !4;                       4 samples with the level inverted
    =2;                       2 samples keeping the current level
    {<0>,l2,h1,}1;            named group "0" (one repetition)
    {<1>,l1,h2,}1;            named group "1"
    b0x55;                    a byte, LSB first, encoded with groups 0/1
    B0x55;                    a byte, MSB first
    s"Hi";                    an ASCII string, LSB first
    [name];                   expand a named group
    {l2,h2,}10;               an anonymous group repeated 10 times

``//`` line comments and ``/* */`` block comments are supported.

Behavioural differences with the original:

* recursive group references are detected and reported instead of overflowing
  the stack;
* the produced sample buffer is a ``numpy`` array;
* the repetition count of a group must be positive.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence

import numpy as np

_NUM = r"(?:0x)?[0-9a-fA-F]+"
_VAL = rf"[hlHL!=]{_NUM}"
_BYTE = rf"[bB]{_NUM}"
_STR = r'[sS]"(?:[^"\\]|\\.)+"'
_NAME = r"\[[0-9a-zA-Z]+\]"

_INITIAL_RE = re.compile(r"^\$[01]$")
_VALUE_RE = re.compile(rf"^{_VAL}$")
_BYTE_RE = re.compile(rf"^{_BYTE}$")
_STRING_RE = re.compile(rf"^{_STR}$")
_NAME_RE = re.compile(rf"^{_NAME}$")
_GROUP_TOKEN = rf"(?:{_NAME}|{_VAL}|{_BYTE}|{_STR})"
# The trailing comma before the closing brace is optional here; the original
# parser required it in some positions and rejected it in others.
_GROUP_RE = re.compile(
    rf"^\{{\s*(?:<(?P<name>[0-9a-zA-Z]+)>\s*,)?"
    rf"(?P<body>(?:\s*{_GROUP_TOKEN}\s*,)*\s*{_GROUP_TOKEN}\s*,?\s*)"
    rf"\}}(?P<repeats>{_NUM})$"
)

_LINE_COMMENT_RE = re.compile(r"//.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

MAX_GROUP_DEPTH = 64


class SDLError(Exception):
    """Base class of every SDL parsing/expansion error."""


class InvalidTokenError(SDLError):
    pass


class InvalidGroupError(SDLError):
    pass


class MissingGroupError(SDLError):
    pass


class DuplicatedGroupError(SDLError):
    pass


class InvalidNumberError(SDLError):
    pass


class TokenType(Enum):
    NONE = "none"
    INITIAL_VALUE = "initial"
    GROUP = "group"
    GROUP_NAME = "group_name"
    VALUE = "value"
    BYTE = "byte"
    STRING = "string"


class ValueTokenType(Enum):
    HIGH = "h"
    LOW = "l"
    INVERT = "!"
    EQUAL = "="


def parse_number(text: str) -> int:
    if not text or text.isspace():
        raise InvalidNumberError(f"Number has an incorrect value: {text}")
    try:
        if text[:2].lower() == "0x":
            if len(text) < 3:
                raise ValueError
            return int(text[2:], 16)
        return int(text, 10)
    except ValueError as error:
        raise InvalidNumberError(f"Cannot parse number: {text}") from error


@dataclass
class Token:
    source: str

    @property
    def token_type(self) -> TokenType:  # pragma: no cover - overridden
        return TokenType.NONE


@dataclass
class InitialToken(Token):
    value: bool = False

    @property
    def token_type(self) -> TokenType:
        return TokenType.INITIAL_VALUE


@dataclass
class ValueToken(Token):
    value_type: ValueTokenType = ValueTokenType.HIGH
    value: int = 0

    @property
    def token_type(self) -> TokenType:
        return TokenType.VALUE


@dataclass
class ByteToken(Token):
    value: int = 0
    lsb_first: bool = True

    @property
    def token_type(self) -> TokenType:
        return TokenType.BYTE


@dataclass
class StringToken(Token):
    value: str = ""
    lsb_first: bool = True

    @property
    def token_type(self) -> TokenType:
        return TokenType.STRING


@dataclass
class GroupNameToken(Token):
    name: str = ""

    @property
    def token_type(self) -> TokenType:
        return TokenType.GROUP_NAME


@dataclass
class GroupToken(Token):
    group_name: Optional[str] = None
    tokens: list[Token] = field(default_factory=list)
    repeats: int = 1

    @property
    def token_type(self) -> TokenType:
        return TokenType.GROUP


def get_token_type(token: str) -> TokenType:
    if _INITIAL_RE.match(token):
        return TokenType.INITIAL_VALUE
    if _VALUE_RE.match(token):
        return TokenType.VALUE
    if _BYTE_RE.match(token):
        return TokenType.BYTE
    if _STRING_RE.match(token):
        return TokenType.STRING
    if _NAME_RE.match(token):
        return TokenType.GROUP_NAME
    if _GROUP_RE.match(token):
        return TokenType.GROUP
    return TokenType.NONE


def _unescape(value: str) -> str:
    replacements = {
        "\\a": "\a", "\\b": "\b", "\\f": "\f", "\\n": "\n", "\\r": "\r",
        "\\t": "\t", "\\v": "\v", "\\\\": "\\", '\\"': '"', "\\;": ";", "\\,": ",",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _make_token(source: str) -> Token:
    token_type = get_token_type(source)

    if token_type == TokenType.INITIAL_VALUE:
        return InitialToken(source=source, value=source[1] == "1")

    if token_type == TokenType.VALUE:
        value = parse_number(source[1:])
        if value < 0:
            raise InvalidTokenError(f"Value out of range (\"{source}\")")
        return ValueToken(
            source=source, value_type=ValueTokenType(source[0].lower() if source[0] in "hHlL" else source[0]),
            value=value,
        )

    if token_type == TokenType.BYTE:
        value = parse_number(source[1:])
        if not 0 <= value <= 255:
            raise InvalidTokenError(f"Byte value out of range (\"{source}\")")
        return ByteToken(source=source, value=value, lsb_first=source[0] == "b")

    if token_type == TokenType.STRING:
        return StringToken(source=source, value=source[2:-1], lsb_first=source[0] == "s")

    if token_type == TokenType.GROUP_NAME:
        return GroupNameToken(source=source, name=source[1:-1])

    if token_type == TokenType.GROUP:
        return _make_group(source)

    raise InvalidTokenError(f'Invalid token found ("{source}").')


def _make_group(source: str) -> GroupToken:
    match = _GROUP_RE.match(source)
    if not match:
        raise InvalidTokenError(f'Token is not a group ("{source}").')

    comma_escape = uuid.uuid4().hex
    body = match.group("body").replace("\\,", comma_escape)
    inner_sources = [
        part.strip().replace(comma_escape, "\\,")
        for part in body.split(",")
        if part.strip()
    ]

    tokens: list[Token] = []
    for inner in inner_sources:
        inner_type = get_token_type(inner)
        if inner_type not in (
            TokenType.VALUE, TokenType.BYTE, TokenType.STRING, TokenType.GROUP_NAME
        ):
            raise InvalidTokenError(f'Invalid token in group ("{inner}").')
        tokens.append(_make_token(inner))

    if not tokens:
        raise InvalidGroupError(f'Group contains no tokens ("{source}").')

    repeats = parse_number(match.group("repeats"))
    if repeats < 1:
        raise InvalidGroupError(f'Group repetition count must be at least 1 ("{source}").')

    return GroupToken(
        source=source, group_name=match.group("name"), tokens=tokens, repeats=repeats
    )


def get_tokens(text: str) -> list[Token]:
    """Tokenize an SDL source."""
    clean = _LINE_COMMENT_RE.sub("", text or "")
    clean = clean.replace("\r", "").replace("\n", "")
    clean = _BLOCK_COMMENT_RE.sub("", clean)

    semicolon_escape = uuid.uuid4().hex
    escaped = clean.replace("\\;", semicolon_escape)

    tokens: list[Token] = []
    for raw in escaped.split(";"):
        source = raw.strip().replace(semicolon_escape, "\\;")
        if not source:
            continue
        tokens.append(_make_token(source))
    return tokens


class TokenizedSDL:
    """A parsed SDL definition that can be expanded into samples."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = get_tokens(source)
        self._validate()

    # ------------------------------------------------------------- accessors
    def _of_type(self, token_type: TokenType) -> list[Token]:
        return [token for token in self.tokens if token.token_type == token_type]

    @property
    def initial_value(self) -> Optional[bool]:
        tokens = self._of_type(TokenType.INITIAL_VALUE)
        return tokens[0].value if tokens else None  # type: ignore[attr-defined]

    @property
    def groups(self) -> list[GroupToken]:
        return [t for t in self.tokens if isinstance(t, GroupToken)]

    @property
    def named_groups(self) -> list[GroupToken]:
        return [group for group in self.groups if group.group_name]

    @property
    def group_names(self) -> list[GroupNameToken]:
        return [t for t in self.tokens if isinstance(t, GroupNameToken)]

    @property
    def bytes_tokens(self) -> list[ByteToken]:
        return [t for t in self.tokens if isinstance(t, ByteToken)]

    @property
    def strings(self) -> list[StringToken]:
        return [t for t in self.tokens if isinstance(t, StringToken)]

    # ------------------------------------------------------------ validation
    def _validate(self) -> None:
        named = {group.group_name for group in self.named_groups}

        missing = [name.name for name in self.group_names if name.name not in named]
        for group in self.groups:
            missing.extend(
                token.name
                for token in group.tokens
                if isinstance(token, GroupNameToken) and token.name not in named
            )
        if missing:
            raise MissingGroupError(f"Missing named groups: {', '.join(sorted(set(missing)))}")

        if self.bytes_tokens or self.strings:
            if "0" not in named or "1" not in named:
                raise MissingGroupError(
                    'Definition contains bytes/strings but groups "0"/"1" are not defined.'
                )

        seen: set[str] = set()
        duplicated: set[str] = set()
        for group in self.named_groups:
            assert group.group_name is not None
            if group.group_name in seen:
                duplicated.add(group.group_name)
            seen.add(group.group_name)
        if duplicated:
            raise DuplicatedGroupError(
                f"Found duplicated named groups: {', '.join(sorted(duplicated))}"
            )

    # ------------------------------------------------------------- expansion
    def to_samples(self, initial_value: Optional[bool] = None) -> np.ndarray:
        """Expand the definition into a ``uint8`` sample array."""
        state = _State(
            level=bool(initial_value if initial_value is not None else (self.initial_value or False)),
            named_groups={group.group_name: group for group in self.named_groups if group.group_name},
        )

        chunks: list[np.ndarray] = []
        for token in self.tokens:
            if isinstance(token, GroupToken) and token.group_name:
                continue  # named groups are definitions, not content
            chunks.append(_expand(token, state, depth=0))

        if not chunks:
            return np.zeros(0, dtype=np.uint8)
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


@dataclass
class _State:
    level: bool
    named_groups: dict[str, GroupToken]


_EMPTY = np.zeros(0, dtype=np.uint8)


def _expand(token: Token, state: _State, depth: int) -> np.ndarray:
    if depth > MAX_GROUP_DEPTH:
        raise InvalidGroupError(
            "Group references are nested too deeply; check for recursive groups."
        )

    if isinstance(token, InitialToken):
        state.level = token.value
        return _EMPTY

    if isinstance(token, ValueToken):
        if token.value_type == ValueTokenType.HIGH:
            state.level = True
        elif token.value_type == ValueTokenType.LOW:
            state.level = False
        elif token.value_type == ValueTokenType.INVERT:
            state.level = not state.level
        return np.full(token.value, 1 if state.level else 0, dtype=np.uint8)

    if isinstance(token, ByteToken):
        return _expand_byte(token.value, token.lsb_first, state, depth)

    if isinstance(token, StringToken):
        data = _unescape(token.value).encode("ascii", "replace")
        chunks = [_expand_byte(value, token.lsb_first, state, depth) for value in data]
        return np.concatenate(chunks) if chunks else _EMPTY

    if isinstance(token, GroupNameToken):
        group = state.named_groups.get(token.name)
        if group is None:
            raise MissingGroupError(f'Cannot find a group named "{token.name}".')
        return _expand_group(group, state, depth + 1)

    if isinstance(token, GroupToken):
        return _expand_group(token, state, depth + 1)

    raise InvalidTokenError(f'Found invalid token: "{token.source}"')


def _expand_group(group: GroupToken, state: _State, depth: int) -> np.ndarray:
    chunks: list[np.ndarray] = []
    for _ in range(group.repeats):
        for token in group.tokens:
            chunks.append(_expand(token, state, depth))
    return np.concatenate(chunks) if chunks else _EMPTY


def _expand_byte(value: int, lsb_first: bool, state: _State, depth: int) -> np.ndarray:
    zero_group = state.named_groups.get("0")
    one_group = state.named_groups.get("1")
    if zero_group is None or one_group is None:
        raise MissingGroupError('Found byte/string value but groups "0"/"1" are missing.')

    chunks: list[np.ndarray] = []
    for bit in range(8):
        mask = (1 << bit) if lsb_first else (128 >> bit)
        group = one_group if value & mask else zero_group
        chunks.append(_expand_group(group, state, depth + 1))
    return np.concatenate(chunks) if chunks else _EMPTY


def samples_from_source(source: str, total_samples: Optional[int] = None) -> np.ndarray:
    """Parse ``source`` and pad/truncate the result to ``total_samples``."""
    samples = TokenizedSDL(source).to_samples()
    if total_samples is None:
        return samples
    if samples.size == 0:
        return np.zeros(total_samples, dtype=np.uint8)
    if samples.size < total_samples:
        padding = np.full(total_samples - samples.size, samples[-1], dtype=np.uint8)
        return np.concatenate((samples, padding))
    return samples[:total_samples]


def describe_tokens(tokens: Iterable[Token]) -> Sequence[str]:  # pragma: no cover - debugging aid
    return [f"{token.token_type.value}: {token.source}" for token in tokens]
