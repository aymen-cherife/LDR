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
import time
import types
from dataclasses import dataclass
from typing import Any

from .node import _SECRET_VALUES, is_secret_name
from .structure import read, type_identity, type_label

UNSUPPORTED = "unsupported"  # a type we do not read structurally (yet)
UNKNOWN = "unknown"  # budget exhausted: we cannot say equal or different
REDACTED = "redacted"  # a secret: no value and no hash is ever kept
FAILED = "failed"  # reading the value raised

STATUSES = (UNSUPPORTED, UNKNOWN, REDACTED, FAILED)

DIGEST_BYTES = 16


@dataclass(frozen=True)
class Budget:
    """Hard per-value limits. Past any of them the result is ``unknown``.

    Defaults come from measurement: about 1 ms per 1,000 nodes, so a value costs
    at most roughly 10 ms (nodes) or 20 ms (time) per recorded call.
    """

    max_nodes: int = 10_000
    max_depth: int = 24
    max_seconds: float = 0.02


DEFAULT_BUDGET = Budget()
_CLOCK_EVERY = 64


class _Stop(Exception):
    def __init__(self, status: str):
        self.status = status


def _looks_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_VALUES)


def is_digest(token: str | None) -> bool:
    return bool(token) and token not in STATUSES


class _Walk:
    def __init__(self, budget: Budget, partial_ok: bool = False) -> None:
        self.budget = budget
        self.partial_ok = partial_ok
        self.partial = False  # some leaf could not be read; only its type is hashed
        self.deadline = time.perf_counter() + budget.max_seconds
        self.active: list[int] = []
        self.visited = 0

    def encode(self, value: Any, out: hashlib.blake2b, depth: int = 0) -> None:
        self.visited += 1
        budget = self.budget
        if depth > budget.max_depth or self.visited > budget.max_nodes:
            raise _Stop(UNKNOWN)
        if not self.visited % _CLOCK_EVERY and time.perf_counter() > self.deadline:
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
            if not self.partial_ok:
                raise _Stop(UNSUPPORTED)
            # A lock, socket or C object: hash its type, say the rest is unread.
            self.partial = True
            self._text(b"?", type_identity(type(value)).encode("utf-8", "surrogatepass"), out)
            return
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


PARTIAL_MARK = "+"


def fingerprint(value: Any, budget: Budget = DEFAULT_BUDGET, *, partial_ok: bool = False) -> str:
    """Return a hex digest of the full value, or a status word. Never raises.

    With ``partial_ok`` a value holding unreadable parts still gets a digest of
    the readable part, followed by ``+`` (used for a call's receiver, which has
    no preview to fall back on). Equal ``+`` digests mean the readable parts
    match; the unread parts are a stated gap.
    """
    try:
        out = hashlib.blake2b(digest_size=DIGEST_BYTES)
        walk = _Walk(budget, partial_ok)
        walk.encode(value, out)
        return out.hexdigest() + (PARTIAL_MARK if walk.partial else "")
    except _Stop as stop:
        return stop.status
    except RecursionError:
        return UNKNOWN
    except Exception:
        return FAILED
