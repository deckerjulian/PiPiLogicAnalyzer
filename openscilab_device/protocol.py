# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The wire format between a remote device and openSciLab (protocol 1, ``docs/remote.md``).

One TCP connection, opened by the device. Every message is a *frame*::

    uint32 header length | uint32 payload length | header (UTF-8 JSON) | payload (bytes)

(big endian). The header is a JSON object with the message type in ``"t"``; the payload carries
the samples of a block (raw little endian numbers of ``dtype``) and is empty otherwise.

Times are seconds as floats. A device stamps what it sends with *its own* clock (a monotonic
clock); openSciLab measures the offset and drift of that clock (``ping``/``pong``) and converts.
The device never needs to know openSciLab's time: commands that should happen at a time carry
that time already converted into the device's clock.

This module has no dependencies beyond the standard library (it runs on a Raspberry Pi as well as
in the application).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import struct
from typing import Any, Optional

PROTOCOL = 1
#: the port openSciLab listens on for remote devices
DEFAULT_PORT = 24050
#: UDP port of the beacon that tells devices where openSciLab is
BEACON_PORT = 24051
BEACON_MAGIC = "openSciLab-remote"
#: a frame larger than this is refused (a broken or hostile peer)
MAX_FRAME = 64 * 1024 * 1024

#: kinds of inputs (what the device measures) and outputs (what openSciLab sets)
INPUT_KINDS = ("analog", "digital", "scalar", "bool", "event", "text")
OUTPUT_KINDS = ("scalar", "bool", "text")
#: inputs that arrive as blocks of samples at a rate
BLOCK_KINDS = ("analog", "digital")
DTYPES = {"f4": 4, "f8": 8, "u1": 1, "i2": 2, "i4": 4}

_HEAD = struct.Struct(">II")


class ProtocolError(Exception):
    """The peer sent something that is not protocol 1, or the connection broke."""


def encode(header: dict, payload: bytes = b"") -> bytes:
    data = json.dumps(header, separators=(",", ":"), allow_nan=True).encode("utf-8")
    return _HEAD.pack(len(data), len(payload)) + data + payload


def _receive_exactly(sock: socket.socket, count: int) -> bytes:
    chunks, remaining = [], count
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ProtocolError("the connection was closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive(sock: socket.socket) -> tuple[dict, bytes]:
    """The next frame of ``sock`` (blocks; raises :class:`ProtocolError` or ``OSError``)."""
    header_length, payload_length = _HEAD.unpack(_receive_exactly(sock, _HEAD.size))
    if header_length > MAX_FRAME or payload_length > MAX_FRAME:
        raise ProtocolError("a frame is too large")
    try:
        header = json.loads(_receive_exactly(sock, header_length).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError(f"not a protocol header: {error}") from None
    if not isinstance(header, dict) or "t" not in header:
        raise ProtocolError("a header without a message type")
    payload = _receive_exactly(sock, payload_length) if payload_length else b""
    return header, payload


def proof(token: str, nonce: str) -> str:
    """The answer to openSciLab's challenge: the token never travels itself."""
    return hmac.new(token.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()


def check_proof(token: str, nonce: str, answer: Optional[str]) -> bool:
    return isinstance(answer, str) and hmac.compare_digest(proof(token, nonce), answer)


def check_description(description: Any) -> dict:
    """The description a device sent, checked (raises :class:`ProtocolError`)."""
    if not isinstance(description, dict) or not str(description.get("name") or "").strip():
        raise ProtocolError("the description has no name")
    names: set[str] = set()
    for section, kinds in (("inputs", INPUT_KINDS), ("outputs", OUTPUT_KINDS)):
        items = description.get(section) or []
        if not isinstance(items, list):
            raise ProtocolError(f"{section} is not a list")
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                raise ProtocolError(f"an entry of {section} has no name")
            if item.get("kind") not in kinds:
                raise ProtocolError(f"{item['name']}: kind {item.get('kind')!r} is not one of {', '.join(kinds)}")
            if section == "inputs" and item["kind"] in BLOCK_KINDS and not (item.get("rate") or 0) > 0:
                raise ProtocolError(f"{item['name']}: a {item['kind']} input needs its rate")
            key = (section, item["name"])
            if str(key) in names:
                raise ProtocolError(f"{item['name']} twice in {section}")
            names.add(str(key))
    for item in description.get("commands") or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise ProtocolError("a command without a name")
    for item in description.get("sync") or []:
        if not isinstance(item, dict) or not item.get("name") or item.get("kind") not in ("output", "input"):
            raise ProtocolError("a sync signal needs a name and the kind output or input")
    return description
