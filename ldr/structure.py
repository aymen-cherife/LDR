"""Read an object's state without running any of its code.

Everything here goes through C-level descriptors (``type.__dict__[...]``,
slot ``member_descriptor`` objects, the built-in ``__dict__`` getter) rather
than ``getattr``/``vars``/``repr``/``str``. A user class that defines
``__repr__``, ``__getattr__``, ``__getstate__``, properties, ``__iter__``,
``__len__``, ``__eq__`` or ``__hash__`` therefore cannot run, block or mutate
anything while LDR records it.
"""

from __future__ import annotations

import types
from typing import Any, NamedTuple

_TYPE_DICT = type.__dict__["__dict__"].__get__
_TYPE_MRO = type.__dict__["__mro__"].__get__
_TYPE_NAME = type.__dict__["__name__"].__get__
_TYPE_QUALNAME = type.__dict__["__qualname__"].__get__
_TYPE_MODULE = type.__dict__["__module__"].__get__
_BASE_EXC_ARGS = BaseException.__dict__["args"].__get__

_BUILTIN_BASES = (dict, list, tuple, set, frozenset)


class Reading(NamedTuple):
    label: str  # class name, for previews
    identity: str  # module + qualified name, for fingerprints
    # (name, value) pairs. ``name`` is None for positional content, such as the
    # items of a list subclass.
    fields: list[tuple[str | None, Any]]


def _safe(call, *args, default=""):
    try:
        result = call(*args)
    except Exception:
        return default
    return result if type(result) is str else default


def type_label(cls: type) -> str:
    return _safe(_TYPE_NAME, cls, default="object")


def type_identity(cls: type) -> str:
    return f"{_safe(_TYPE_MODULE, cls)}.{_safe(_TYPE_QUALNAME, cls, default=type_label(cls))}"


def instance_dict(value: Any) -> dict | None:
    """The instance ``__dict__`` through the built-in getter only, else None."""
    try:
        for klass in _TYPE_MRO(type(value)):
            descriptor = _TYPE_DICT(klass).get("__dict__")
            if type(descriptor) is types.GetSetDescriptorType:
                state = descriptor.__get__(value)
                return state if type(state) is dict else None
    except Exception:
        return None
    return None


def _slot_names(klass: type) -> list[str]:
    slots = _TYPE_DICT(klass).get("__slots__")
    if type(slots) is str:
        names = [slots]
    elif type(slots) in (tuple, list):
        names = [name for name in slots if type(name) is str]
    else:
        return []
    return [name for name in names if name not in ("__dict__", "__weakref__")]


def _slot_fields(value: Any, mro: tuple) -> list[tuple[str, Any]]:
    fields: list[tuple[str, Any]] = []
    for klass in reversed(mro):
        class_dict = _TYPE_DICT(klass)
        for name in _slot_names(klass):
            stored = name
            if name.startswith("__") and not name.endswith("__"):
                stored = "_" + type_label(klass).lstrip("_") + name
            descriptor = class_dict.get(stored)
            if type(descriptor) is not types.MemberDescriptorType:
                continue
            try:
                fields.append((name, descriptor.__get__(value)))
            except AttributeError:
                continue  # slot declared but never assigned
    return fields


def _builtin_content(value: Any, base: type) -> list[tuple[str | None, Any]]:
    """Items of a builtin-container subclass, read with the base type's methods."""
    if base is dict:
        return [(None, pair) for pair in tuple(dict.items(value))]
    if base is list:
        return [(None, item) for item in tuple(list.__iter__(value))]
    if base is tuple:
        return [(None, item) for item in tuple.__iter__(value)]
    iterator = set.__iter__ if base is set else frozenset.__iter__
    return [(None, item) for item in tuple(iterator(value))]


def read(value: Any) -> Reading | None:
    """Structural reading of a non-builtin instance, or None if unreadable."""
    cls = type(value)
    try:
        mro = _TYPE_MRO(cls)
        fields: list[tuple[str | None, Any]] = []

        for base in _BUILTIN_BASES:
            if base in mro:
                names = None
                for klass in mro:
                    candidate = _TYPE_DICT(klass).get("_fields")
                    if type(candidate) is tuple and all(type(n) is str for n in candidate):
                        names = candidate
                        break
                content = _builtin_content(value, base)
                if names is not None and base is tuple and len(names) == len(content):
                    fields = [(name, item) for name, (_, item) in zip(names, content, strict=True)]
                else:
                    fields = content
                break

        if BaseException in mro:
            fields.append(("args", _BASE_EXC_ARGS(value)))

        fields.extend(_slot_fields(value, mro))
        state = instance_dict(value)
        if state is not None:
            fields.extend((key, item) for key, item in tuple(state.items()) if type(key) is str)

        if not fields and state is None and not _has_state_layout(mro):
            return None
        return Reading(type_label(cls), type_identity(cls), fields)
    except Exception:
        return None


def _has_state_layout(mro: tuple) -> bool:
    """True for ordinary Python classes (an empty ``object`` has no state to hide)."""
    for klass in mro:
        if klass is object:
            continue
        if type(_TYPE_DICT(klass).get("__dict__")) is types.GetSetDescriptorType:
            return True
        if "__slots__" in _TYPE_DICT(klass):
            return True
    return len(mro) == 1  # ``object()`` itself
