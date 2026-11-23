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
import functools
import hashlib
import types
from dataclasses import dataclass
import os
import re
import struct
import tempfile
from typing import Any

from .redact import contains_secret, is_secret_name, same_text_policy, text_policy
from .structure import read, type_identity, type_label

UNSUPPORTED = "unsupported"  # a type we do not read structurally (yet)
UNKNOWN = "unknown"  # budget exhausted: we cannot say equal or different
REDACTED = "redacted"  # a secret: no value and no hash is ever kept
FAILED = "failed"  # reading the value raised

STATUSES = (UNSUPPORTED, UNKNOWN, REDACTED, FAILED)

DIGEST_BYTES = 16


@dataclass(frozen=True)
class Budget:
    """Per-value limits. Past one of them the result is ``unknown``.

    Deliberately no time budget: a wall-clock limit would make the same value
    ``complete`` in one run and ``unknown`` in the next, depending on machine
    load, and fake a difference. There is no node cap by default (Phase 2 rule
    5): a large value is walked, and the cost is reported per function. The
    depth limit stays as a guard. ``max_nodes`` remains for callers that set one.
    """

    max_nodes: int | None = None
    max_depth: int = 24


DEFAULT_BUDGET = Budget()


class _Stop(Exception):
    def __init__(self, status: str):
        self.status = status


# Remembered answers for small immutable values, in memory only. Each cache is
# bounded and is emptied whenever the redaction inputs (environment or argv
# secrets) change, so a remembered answer is always the answer the uncached path
# would give now.
CACHE = True
CACHE_LIMIT = 65_536
SHORT_TEXT = 256
_secret_cache: dict[str, bool] = {}
_value_cache: dict = {}
_cache_policy = None


def _current_caches():
    global _cache_policy
    if _cache_policy is None or not same_text_policy(_cache_policy):
        _secret_cache.clear()
        _value_cache.clear()
        _cache_policy = text_policy()


def _looks_secret(text: str) -> bool:
    if not CACHE or len(text) > SHORT_TEXT:
        return contains_secret(text)
    _current_caches()
    found = _secret_cache.get(text)
    if found is None:
        found = contains_secret(text)
        if len(_secret_cache) >= CACHE_LIMIT:
            _secret_cache.clear()
        _secret_cache[text] = found
    return found


def _scalar_key(value):
    """A key that separates every value the walk separates: type plus value.

    Floats are keyed by their bytes, so 0.0 and -0.0 differ and NaN equals
    itself. ``True`` and ``1`` differ because the type is part of the key.
    """
    kind = type(value)
    if kind is int:
        return (int, value) if -(1 << 63) <= value < (1 << 63) else None
    if kind is bool or value is None:
        return (kind, value)
    if kind is float:
        return (float, struct.pack("<d", value))
    if kind is str:
        return (str, value) if len(value) <= SHORT_TEXT else None
    return None


def _cache_key(value):
    if type(value) is tuple:
        if len(value) > 8:
            return None
        keys = []
        for item in value:
            key = _scalar_key(item)
            if key is None:
                return None
            keys.append(key)
        return (tuple, tuple(keys))
    return _scalar_key(value)


def is_digest(token: str | None) -> bool:
    return bool(token) and token not in STATUSES


class _Stats:
    """Process-wide counters, read by the recorder to report cost and gaps."""

    __slots__ = ("nodes", "tmp_normalized")

    def __init__(self) -> None:
        self.nodes = 0
        self.tmp_normalized = 0


STATS = _Stats()

# A homogeneous list or tuple at least this long is encoded without a Python call per item.
FAST_PATH_MIN = 32

_TMP_PLACEHOLDER = "<tmp>/…"


def _temp_prefixes() -> list[str]:
    root = tempfile.gettempdir()
    found = {root.rstrip("\\/"), os.path.realpath(root).rstrip("\\/")}
    return sorted((p for p in found if p), key=len, reverse=True)


_TMP_PREFIXES = _temp_prefixes()
_TMP_FOLD = os.name == "nt"
_TMP_MIN = min((len(p) for p in _TMP_PREFIXES), default=1 << 30)
_TMP_RE = (
    re.compile(
        "(?:" + "|".join(re.escape(p) for p in _TMP_PREFIXES) + ")"
        + r"""(?:[\\/][^\s'"]*)?(?=[\s'"]|$)""",
        re.IGNORECASE if _TMP_FOLD else 0,
    )
    if _TMP_PREFIXES
    else None
)
_TMP_HINTS = [p.casefold() if _TMP_FOLD else p for p in _TMP_PREFIXES]


def normalize_temp_paths(text: str) -> str:
    """Replace every path under the temp directory with ``<tmp>/…``.

    A temp path carries a name that differs on every run, so it could never
    compare equal. Differences inside such a path are therefore not compared, and
    the count is reported in the gaps line.
    """
    if _TMP_RE is None or len(text) < _TMP_MIN:
        return text
    probe = text.casefold() if _TMP_FOLD else text
    if not any(hint in probe for hint in _TMP_HINTS):
        return text
    result, count = _TMP_RE.subn(_TMP_PLACEHOLDER, text)
    STATS.tmp_normalized += count
    return result


class _Walk:
    def __init__(self, budget: Budget, partial_ok: bool = False) -> None:
        self.budget = budget
        self.partial_ok = partial_ok
        self.partial = False  # some leaf could not be read; only its type is hashed
        self.immutable = True
        self.active: list[int] = []
        self.visited = 0

    def encode(self, value: Any, out: hashlib.blake2b, depth: int = 0) -> None:
        self.visited += 1
        budget = self.budget
        if depth > budget.max_depth or (
            budget.max_nodes is not None and self.visited > budget.max_nodes
        ):
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
            self._text(b"s", normalize_temp_paths(value).encode("utf-8", "surrogatepass"), out)
        elif kind is bytes or kind is bytearray:
            if kind is bytearray:
                self.immutable = False
            if _looks_secret(bytes(value).decode("latin-1")):
                raise _Stop(REDACTED)
            self._text(b"b" if kind is bytes else b"B", bytes(value), out)
        elif kind is decimal.Decimal:
            self.immutable = False
            self._text(b"D", str(value).encode(), out)
        elif kind in (list, tuple, dict, set, frozenset):
            if kind is not tuple and kind is not frozenset:
                self.immutable = False
            self._container(kind, value, out, depth)
        else:
            self.immutable = False
            if not self._known_leaf(kind, value, out, depth):
                self._object(value, out, depth)

    def _known_leaf(self, kind: type, value: Any, out: hashlib.blake2b, depth: int) -> bool:
        """Exact standard types whose state we can read without user code."""
        if kind is type or kind is abc.ABCMeta:
            self._text(b"C", type_identity(value).encode(), out)
        elif kind is types.FunctionType or kind is types.BuiltinFunctionType:
            name = f"{value.__module__}.{value.__qualname__}"
            # A name, never a read: for a built-in bound method (``[].append``) that is the
            # name only, not the object it is bound to.
            self._text(b"Y", name.encode("utf-8", "surrogatepass"), out)
        elif kind is types.MethodType:
            # The function's name and the receiver's type. The receiver itself is not read:
            # a method is a way to call something, not a value to compare.
            out.update(b"Mt")
            self.encode(value.__func__, out, depth + 1)
            self._text(b"T", type_identity(type(value.__self__)).encode("utf-8", "surrogatepass"), out)
        elif kind is functools.partial:
            # The wrapped callable and every bound argument, read like any other value:
            # secrets in them are redacted the same way. Keywords are an unordered set
            # of names, so they are sorted.
            out.update(b"Pa")
            self.encode(value.func, out, depth + 1)
            self.encode(value.args, out, depth + 1)
            out.update(str(len(value.keywords)).encode() + b"{")
            for key in sorted(value.keywords):
                if is_secret_name(key):
                    raise _Stop(REDACTED)
                self._text(b"n", key.encode("utf-8", "surrogatepass"), out)
                self.encode(value.keywords[key], out, depth + 1)
            out.update(b"}")
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

    def _fast_items(self, value: Any, out: hashlib.blake2b, depth: int) -> bool:
        """Encode a homogeneous run of ints, floats, bools or strings in bulk.

        The bytes are the same as the per-item walk would write. Returns False,
        having written nothing, when the items are not all one of those types.
        """
        kinds = set(map(type, value))
        if len(kinds) != 1:
            return False
        item_kind = next(iter(kinds))
        if item_kind is int:
            body = "".join(["i%x;" % item for item in value]).encode()
        elif item_kind is float:
            body = "".join(["f" + item.hex() + ";" for item in value]).encode()
        elif item_kind is bool:
            body = "".join(["T" if item else "F" for item in value]).encode()
        elif item_kind is str:
            pieces = []
            for item in value:
                if _looks_secret(item):
                    raise _Stop(REDACTED)
                data = normalize_temp_paths(item).encode("utf-8", "surrogatepass")
                pieces.append(b"s" + str(len(data)).encode() + b":" + data)
            body = b"".join(pieces)
        else:
            return False
        if depth + 1 > self.budget.max_depth:
            raise _Stop(UNKNOWN)
        self.visited += len(value)
        if self.budget.max_nodes is not None and self.visited > self.budget.max_nodes:
            raise _Stop(UNKNOWN)
        out.update(body)
        return True

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
                if len(value) < FAST_PATH_MIN or not self._fast_items(value, out, depth):
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
    return fingerprint_details(value, budget, partial_ok=partial_ok)[0]


def fingerprint_details(
    value: Any, budget: Budget = DEFAULT_BUDGET, *, partial_ok: bool = False
) -> tuple[str, bool]:
    """Return the existing fingerprint and whether it can be reused for this object."""
    if CACHE and budget is DEFAULT_BUDGET:
        key = _cache_key(value)
        if key is not None:
            _current_caches()
            key = (key, partial_ok)
            found = _value_cache.get(key)
            if found is not None:
                return found
            normalized = STATS.tmp_normalized
            found = _fingerprint_details(value, budget, partial_ok)
            # A normalized temp path is counted for the gaps line on every
            # occurrence, so it is never answered from the cache.
            if STATS.tmp_normalized == normalized:
                if len(_value_cache) >= CACHE_LIMIT:
                    _value_cache.clear()
                _value_cache[key] = found
            return found
    return _fingerprint_details(value, budget, partial_ok)


def _fingerprint_details(value: Any, budget: Budget, partial_ok: bool) -> tuple[str, bool]:
    walk = None
    try:
        out = hashlib.blake2b(digest_size=DIGEST_BYTES)
        walk = _Walk(budget, partial_ok)
        walk.encode(value, out)
        return out.hexdigest() + (PARTIAL_MARK if walk.partial else ""), (
            walk.immutable and not walk.partial
        )
    except _Stop as stop:
        return stop.status, False
    except RecursionError:
        return UNKNOWN, False
    except Exception:
        return FAILED, False
    finally:
        if walk is not None:
            STATS.nodes += walk.visited
