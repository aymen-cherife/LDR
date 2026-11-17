"""Value fingerprints: a stable digest of the *full* value, never of its preview.

A fingerprint is computed from the value itself, in every mode, by walking
built-in containers structurally. It never calls user code, so it cannot
mutate, block or do work the way ``repr`` can.

A result is either a hex digest or one of the status words below. Only digests
can be compared for equality; any other status means "not compared" or
"unknown", never "equal".
"""

from __future__ import annotations

import abc
import datetime
import decimal
import hashlib
import types
from typing import Any

from .node import _SECRET_VALUES, is_secret_name
from .structure import read, type_identity, type_label

UNSUPPORTED = "unsupported"  # a type we do not read structurally (yet)
UNKNOWN = "unknown"  # budget exhausted: we cannot say equal or different
REDACTED = "redacted"  # a secret: no value and no hash is ever kept
FAILED = "failed"  # reading the value raised

STATUSES = (UNSUPPORTED, UNKNOWN, REDACTED, FAILED)

MAX_DEPTH = 24
MAX_NODES = 50_000  # item and time budgets are tuned in the budget commit
DIGEST_BYTES = 16


class _Stop(Exception):
    def __init__(self, status: str):
        self.status = status


def _looks_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_VALUES)


def is_digest(token: str | None) -> bool:
    return bool(token) and token not in STATUSES


class _Walk:
    def __init__(self) -> None:
        self.active: list[int] = []
        self.visited = 0

    def encode(self, value: Any, out: hashlib.blake2b, depth: int = 0) -> None:
        self.visited += 1
        if depth > MAX_DEPTH or self.visited > MAX_NODES:
            raise _Stop(UNKNOWN)
        kind = type(value)
        if value is None:
            out.update(b"N")
        elif kind is bool:
            out.update(b"T" if value else b"F")
        elif kind is int:
            out.update(b"i" + format(value, "x").encode() + b";")
        elif kind is float:
            out.update(b"f" + value.hex().encode() + b";")
        elif kind is complex:
            out.update(b"c" + value.real.hex().encode() + b"," + value.imag.hex().encode() + b";")
        elif kind is str:
            if _looks_secret(value):
                raise _Stop(REDACTED)
            self._text(b"s", value.encode("utf-8", "surrogatepass"), out)
        elif kind is bytes:
            self._text(b"b", value, out)
        elif kind is bytearray:
            self._text(b"B", bytes(value), out)
        elif kind is decimal.Decimal:
            self._text(b"D", str(value).encode(), out)
        elif kind in (list, tuple, dict, set, frozenset):
            self._container(kind, value, out, depth)
        elif not self._known_leaf(kind, value, out, depth):
            self._object(value, out, depth)

    def _known_leaf(self, kind: type, value: Any, out: hashlib.blake2b, depth: int) -> bool:
        """Exact standard types whose state we can read without user code."""
        if kind is type or kind is abc.ABCMeta:
            self._text(b"C", type_identity(value).encode(), out)
        elif kind is types.FunctionType or kind is types.BuiltinFunctionType:
            name = f"{value.__module__}.{value.__qualname__}"
            self._text(b"Y", name.encode("utf-8", "surrogatepass"), out)
        elif kind is types.MethodType:
            out.update(b"Mt")
            self.encode(value.__func__, out, depth + 1)
            self.encode(value.__self__, out, depth + 1)
        elif kind in (datetime.datetime, datetime.date, datetime.time):
            self._text(b"M", (type_label(kind) + value.isoformat()).encode(), out)
        elif kind is datetime.timedelta:
            self._text(b"M", f"td{value.days},{value.seconds},{value.microseconds}".encode(), out)
        elif kind is range:
            self._text(b"R", f"{value.start},{value.stop},{value.step}".encode(), out)
        elif value is Ellipsis:
            out.update(b"E")
        elif value is NotImplemented:
            out.update(b"X")
        else:
            return False
        return True

    def _object(self, value: Any, out: hashlib.blake2b, depth: int) -> None:
        reading = read(value)
        if reading is None:
            raise _Stop(UNSUPPORTED)
        marker = id(value)
        if marker in self.active:
            out.update(b"@" + str(len(self.active) - self.active.index(marker)).encode() + b";")
            return
        self.active.append(marker)
        try:
            self._text(b"O", reading.identity.encode("utf-8", "surrogatepass"), out)
            out.update(str(len(reading.fields)).encode() + b"{")
            for name, item in reading.fields:
                if name is not None:
                    if is_secret_name(name):
                        raise _Stop(REDACTED)
                    self._text(b"n", name.encode("utf-8", "surrogatepass"), out)
                self.encode(item, out, depth + 1)
            out.update(b"}")
        finally:
            self.active.pop()

    @staticmethod
    def _text(tag: bytes, data: bytes, out: hashlib.blake2b) -> None:
        out.update(tag + str(len(data)).encode() + b":" + data)

    def _container(self, kind: type, value: Any, out: hashlib.blake2b, depth: int) -> None:
        marker = id(value)
        if marker in self.active:
            # A cycle: refer to the ancestor by distance, not by address.
            out.update(b"@" + str(len(self.active) - self.active.index(marker)).encode() + b";")
            return
        self.active.append(marker)
        try:
            if kind is list or kind is tuple:
                out.update((b"l" if kind is list else b"t") + str(len(value)).encode() + b"[")
                for item in value:
                    self.encode(item, out, depth + 1)
            elif kind is dict:
                # Insertion order is part of the value.
                out.update(b"d" + str(len(value)).encode() + b"{")
                for key, item in value.items():
                    if is_secret_name(key):
                        raise _Stop(REDACTED)
                    self.encode(key, out, depth + 1)
                    self.encode(item, out, depth + 1)
            else:
                # Sets have no order: sort the element digests.
                parts = []
                for item in value:
                    part = hashlib.blake2b(digest_size=DIGEST_BYTES)
                    self.encode(item, part, depth + 1)
                    parts.append(part.digest())
                parts.sort()
                out.update((b"e" if kind is set else b"z") + str(len(parts)).encode() + b"(")
                for part in parts:
                    out.update(part)
            out.update(b"]")
        finally:
            self.active.pop()


def fingerprint(value: Any) -> str:
    """Return a hex digest of the full value, or a status word. Never raises."""
    try:
        out = hashlib.blake2b(digest_size=DIGEST_BYTES)
        _Walk().encode(value, out)
        return out.hexdigest()
    except _Stop as stop:
        return stop.status
    except RecursionError:
        return UNKNOWN
    except Exception:
        return FAILED
