# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""What travels between the application and a device process (``docs/timing.md``, 4b).

Messages are pickled with :mod:`openscilab.core.shared_arrays` (samples as references to shared
memory) and these references besides:

* ``("object", id)`` - an object living in the device process (a facet, a sub-driver, a function
  it returned); the application gets a :class:`~.proxy.RemoteObject` for it;
* ``("callback", id)`` - a function of the application (a handler); the device process calls a
  stub that sends an event;
* ``("session", id)`` - the capture session of a running capture, the same object on each side.

Qt free.
"""

from __future__ import annotations

import importlib
import io
import pickle
import traceback
from typing import Any, Callable, Optional

from ...core import shared_arrays

#: object ids of every device process
INSTRUMENT = 0
DRIVER = 1


def members_of(obj: Any) -> dict[str, str]:
    """The public members of ``obj``: ``"method"`` or ``"attr"`` (nothing is evaluated)."""
    import inspect

    found: dict[str, str] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            static = inspect.getattr_static(obj, name)
        except AttributeError:
            continue
        if isinstance(static, (property, staticmethod)) or not callable(static):
            if isinstance(static, staticmethod):
                found[name] = "method"
            else:
                found[name] = "attr"
            continue
        found[name] = "method"
    return found


def constants_of(obj: Any, members: dict[str, str]) -> dict[str, Any]:
    """The attributes of ``obj`` its class defines as plain values (``title``): they do not change."""
    import inspect

    found = {}
    for name, kind in members.items():
        if kind != "attr" or name in getattr(obj, "__dict__", {}):
            continue
        try:
            value = inspect.getattr_static(type(obj), name)
        except AttributeError:
            continue
        if isinstance(value, (property, staticmethod, classmethod)) or callable(value):
            continue
        try:
            pickle.dumps(value)
        except Exception:  # (a probe, not an error)
            continue
        found[name] = value
    return found


def class_paths(obj: Any) -> list[str]:
    """``module:qualname`` of the classes of ``obj`` (its MRO, up to ``object``)."""
    return [f"{cls.__module__}:{cls.__qualname__}" for cls in type(obj).__mro__ if cls is not object]


def load_class(path: str) -> Optional[type]:
    module, _, name = path.partition(":")
    try:
        found: Any = importlib.import_module(module)
        for part in name.split("."):
            found = getattr(found, part)
        return found if isinstance(found, type) else None
    except (ImportError, AttributeError):
        return None


def is_handler(obj: Any) -> bool:
    """A function that goes by reference: a bound method, a closure or a lambda. Functions of a module
    go by name - pickle itself names its helpers (``copyreg.__newobj__``) that way."""
    if isinstance(obj, type) or not callable(obj):
        return False
    kind = type(obj).__name__
    if kind in ("method", "method-wrapper"):
        return True
    if kind == "builtin_function_or_method":
        owner = getattr(obj, "__self__", None)
        return owner is not None and not isinstance(owner, type(pickle))  # bound to an object, not a module
    if kind == "function":
        qualname = getattr(obj, "__qualname__", "")
        return "<locals>" in qualname or "<lambda>" in qualname
    return kind == "partial"


# ----------------------------------------------------------------- errors
def pack_error(error: BaseException) -> tuple:
    state: dict = {}
    for key, value in getattr(error, "__dict__", {}).items():
        try:
            pickle.dumps(value)
            state[key] = value
        except Exception:  # (a probe, not an error)
            continue
    try:
        pickle.dumps(error.args)
        args = error.args
    except Exception:  # (a probe, not an error)
        args = tuple(str(arg) for arg in error.args)
    return (type(error).__module__, type(error).__qualname__, args, state,
            "".join(traceback.format_exception(type(error), error, error.__traceback__)))


def unpack_error(packed: tuple) -> BaseException:
    module, name, args, state, text = packed
    cls = load_class(f"{module}:{name}")
    if cls is None or not issubclass(cls, BaseException):
        error: BaseException = RuntimeError(f"{name}: {', '.join(str(arg) for arg in args)}")
    else:
        error = cls.__new__(cls)
        error.args = args
        try:
            error.__dict__.update(state)
        except AttributeError:
            pass
    error.remote_traceback = text  # type: ignore[attr-defined]
    return error


# ---------------------------------------------------------------- pickling
class Pickler(pickle.Pickler):
    """Shared arrays as references; ``reference(obj)`` names other objects (or ``None``)."""

    def __init__(self, file, keep: list, reference: Callable[[Any], Optional[tuple]]) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.keep = keep
        self.reference = reference
        self.arrays = shared_arrays._Pickler(io.BytesIO(), keep)

    def persistent_id(self, obj: Any) -> Any:
        found = self.reference(obj)
        if found is not None:
            return found
        return self.arrays.persistent_id(obj)


class Unpickler(pickle.Unpickler):
    def __init__(self, file, resolve: Callable[[tuple], Any]) -> None:
        super().__init__(file)
        self.resolve = resolve

    def persistent_load(self, pid: Any) -> Any:
        if pid[0] == "array":
            return shared_arrays.open_array(pid[1])
        return self.resolve(pid)


def dumps(obj: Any, reference: Callable[[Any], Optional[tuple]]) -> tuple[bytes, list]:
    buffer = io.BytesIO()
    keep: list = []
    Pickler(buffer, keep, reference).dump(obj)
    return buffer.getvalue(), keep


def loads(data: bytes, resolve: Callable[[tuple], Any]) -> Any:
    return Unpickler(io.BytesIO(data), resolve).load()


def adopt(target: Any, source: Any) -> None:
    """Copy the state of a capture session that came back into the application's own object (the
    one its windows and flows hold), its channels in place."""
    for name, value in vars(source).items():
        if name in ("capture_channels", "analog_channels"):
            current = getattr(target, name, None)
            if isinstance(current, list) and len(current) == len(value) and all(
                    getattr(old, "channel_number", None) == getattr(new, "channel_number", None)
                    for old, new in zip(current, value)):
                for old, new in zip(current, value):
                    old.__dict__.update(vars(new))
                continue
        setattr(target, name, value)
