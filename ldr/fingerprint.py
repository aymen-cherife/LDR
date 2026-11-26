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
    load, and fake a difference. Only the comparison walk can use a node ceiling.
    """

    max_nodes: int | None = None


DEFAULT_BUDGET = Budget()

# The node ceiling of default mode (close-list item 4). A value that needs more nodes than
# this is not walked in full: it gets a cheap sketch instead (type, size, top-level parts),
# marked with SKETCH_MARK. None means no ceiling (--full= and --deep=), and a function
# named by --uncap runs with the ceiling lifted. TO MEASURE: the default is a placeholder.
DEFAULT_WALK_CEILING = 20_000
_ceiling: Budget | None = None
_lifted = 0


def configure(ceiling: int | None) -> None:
    """Set the node ceiling for this process; None or 0 removes it."""
    global _ceiling
    _ceiling = Budget(max_nodes=ceiling) if ceiling else None


class lifted:
    """``with lifted():`` computes fingerprints without the node ceiling."""

    def __enter__(self):
        global _lifted
        _lifted += 1

    def __exit__(self, *exc):
        global _lifted
        _lifted -= 1


def _effective() -> Budget:
    return _ceiling if _ceiling is not None and not _lifted else DEFAULT_BUDGET


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
        _identity_cache.clear()
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

    __slots__ = ("nodes", "tmp_normalized", "last_visited", "walks", "walked", "walked_type")

    def __init__(self) -> None:
        self.nodes = 0
        self.tmp_normalized = 0
        self.last_visited = 0
        # The latest walk: how many there have been, its node count and the type of its value.
        # The recorder reads them right after a fingerprint to log the size (batch 1).
        self.walks = 0
        self.walked = 0
        self.walked_type = None


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
        self.active: dict[int, int] = {}
        self.visited = 0

    def encode(self, value: Any, out: hashlib.blake2b) -> None:
        stack = [iter(((value, out),))]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                stack.pop()
            else:
                children = self._encode(*child)
                if children is not None:
                    stack.append(children)

    def _encode(self, value: Any, out: hashlib.blake2b):
        self.visited += 1
        budget = self.budget
        if budget.max_nodes is not None and self.visited > budget.max_nodes:
            raise _Stop(UNKNOWN)
        kind = type(value)
        if value is None:
            out.update(b"N")
        elif kind is bool:
            out.update(b"T" if value else b"F")
        elif kind is int:
            out.update(f"i{value:x};".encode())
        elif kind is float:
            out.update(f"f{value.hex()};".encode())
        elif kind is complex:
            out.update(f"c{value.real.hex()},{value.imag.hex()};".encode())
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
            return self._container(kind, value, out)
        else:
            self.immutable = False
            if kind is types.MethodType or kind is functools.partial:
                return self._callable(kind, value, out)
            if not self._known_leaf(kind, value, out):
                return self._object(value, out)
        return None

    def _known_leaf(self, kind: type, value: Any, out: hashlib.blake2b) -> bool:
        """Exact standard types whose state we can read without user code."""
        if kind is type or kind is abc.ABCMeta:
            self._text(b"C", type_identity(value).encode(), out)
        elif kind is types.FunctionType or kind is types.BuiltinFunctionType:
            name = f"{value.__module__}.{value.__qualname__}"
            # A name, never a read: for a built-in bound method (``[].append``) that is the
            # name only, not the object it is bound to.
            self._text(b"Y", name.encode("utf-8", "surrogatepass"), out)
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

    def _callable(self, kind, value, out):
        if kind is types.MethodType:
            # The function's name and the receiver's type. The receiver itself is not read:
            # a method is a way to call something, not a value to compare.
            out.update(b"Mt")
            yield value.__func__, out
            self._text(b"T", type_identity(type(value.__self__)).encode("utf-8", "surrogatepass"), out)
        else:
            # The wrapped callable and every bound argument, read like any other value:
            # secrets in them are redacted the same way. Keywords are an unordered set
            # of names, so they are sorted.
            out.update(b"Pa")
            yield value.func, out
            yield value.args, out
            out.update(b"%d{" % len(value.keywords))
            for key in sorted(value.keywords):
                if is_secret_name(key):
                    raise _Stop(REDACTED)
                self._text(b"n", key.encode("utf-8", "surrogatepass"), out)
                yield value.keywords[key], out
            out.update(b"}")

    def _object(self, value: Any, out: hashlib.blake2b):
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
            out.update(b"@%d;" % (len(self.active) - self.active[marker]))
            return
        self.active[marker] = len(self.active)
        try:
            self._text(b"O", reading.identity.encode("utf-8", "surrogatepass"), out)
            out.update(b"%d{" % len(reading.fields))
            for name, item in reading.fields:
                if name is not None:
                    if is_secret_name(name):
                        raise _Stop(REDACTED)
                    self._text(b"n", name.encode("utf-8", "surrogatepass"), out)
                yield item, out
            out.update(b"}")
        finally:
            del self.active[marker]

    def _fast_items(self, value: Any, out: hashlib.blake2b) -> bool:
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
                pieces.append(b"s%d:%s" % (len(data), data))
            body = b"".join(pieces)
        else:
            return False
        self.visited += len(value)
        if self.budget.max_nodes is not None and self.visited > self.budget.max_nodes:
            raise _Stop(UNKNOWN)
        out.update(body)
        return True

    @staticmethod
    def _text(tag: bytes, data: bytes, out: hashlib.blake2b) -> None:
        out.update(b"%s%d:%s" % (tag, len(data), data))

    def _container(self, kind: type, value: Any, out: hashlib.blake2b):
        marker = id(value)
        if marker in self.active:
            # A cycle: refer to the ancestor by distance, not by address.
            out.update(b"@%d;" % (len(self.active) - self.active[marker]))
            return
        self.active[marker] = len(self.active)
        try:
            if kind is list or kind is tuple:
                out.update((b"l%d[" if kind is list else b"t%d[") % len(value))
                if len(value) < FAST_PATH_MIN or not self._fast_items(value, out):
                    for item in value:
                        yield item, out
            elif kind is dict:
                # Insertion order is part of the value.
                out.update(b"d%d{" % len(value))
                for key, item in value.items():
                    if is_secret_name(key):
                        raise _Stop(REDACTED)
                    yield key, out
                    yield item, out
            else:
                # Sets have no order: sort the element digests.
                parts = []
                for item in value:
                    part = hashlib.blake2b(digest_size=DIGEST_BYTES)
                    yield item, part
                    parts.append(part.digest())
                parts.sort()
                out.update((b"e%d(" if kind is set else b"z%d(") % len(parts))
                for part in parts:
                    out.update(part)
            out.update(b"]")
        finally:
            del self.active[marker]


PARTIAL_MARK = "+"
SKETCH_MARK = "~"  # the digest is of a sketch: equal does not prove the value unchanged
SKETCH_TYPES: dict[str, str] = {}  # sketch token -> type label, for the gaps line (bounded)
SKETCH_TYPES_LIMIT = 2000


def is_sketch(token: str | None) -> bool:
    return bool(token) and token.endswith(SKETCH_MARK)


def _shallow(item: Any) -> str:
    """One part of a sketch: its type and size, and the value itself when it is a small scalar."""
    kind = type(item)
    label = type_identity(kind)
    if kind in (int, float, bool) or item is None:
        return f"{label}={item!r}"
    if kind in (str, bytes, bytearray, list, tuple, dict, set, frozenset):
        return f"{label}[{len(item)}]"
    return label


def _sketch(value: Any) -> str:
    """A digest of a value's type, size and top-level parts. Never calls user code."""
    kind = type(value)
    parts: list[str] = [type_identity(kind)]
    if kind in (list, tuple):
        parts.append(f"len={len(value)}")
        parts += [_shallow(item) for item in value[:32]]
        parts.append("...")
        parts += [_shallow(item) for item in value[-32:]]
    elif kind is dict:
        parts.append(f"len={len(value)}")
        for key, item in list(value.items())[:64]:
            if type(key) is str and len(key) <= 32 and not _looks_secret(key) and not is_secret_name(key):
                parts.append(f"{key}:{_shallow(item)}")
            else:
                parts.append(f"{_shallow(key)}:{_shallow(item)}")
    elif kind in (set, frozenset):
        parts.append(f"len={len(value)}")
        parts += sorted(_shallow(item) for item in list(value)[:64])
    else:
        reading = read(value)
        if reading is not None:
            parts.append(f"fields={len(reading.fields)}")
            for name, item in reading.fields[:64]:
                if name is not None and is_secret_name(name):
                    raise _Stop(REDACTED)
                parts.append(f"{name}:{_shallow(item)}")
        elif hasattr(kind, "__len__"):
            parts.append("sized")
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8", "surrogatepass"), digest_size=DIGEST_BYTES)
    token = digest.hexdigest() + SKETCH_MARK
    if len(SKETCH_TYPES) >= SKETCH_TYPES_LIMIT:
        SKETCH_TYPES.clear()
    SKETCH_TYPES[token] = type_label(kind)
    return token


# Deeply immutable values (nothing mutable at any depth) keep their fingerprint between
# calls. Each entry holds a reference to the value, so the value's id cannot be reused by
# another object while it is cached; the cache is small and first-in first-out.
IDENTITY_LIMIT = 512
IDENTITY_MIN_NODES = 16
_identity_cache: dict[tuple[int, bool], tuple[Any, tuple[str, bool]]] = {}


def fingerprint(value: Any, budget: Budget | None = None, *, partial_ok: bool = False) -> str:
    """Return a hex digest of the full value, or a status word. Never raises.

    With ``partial_ok`` a value holding unreadable parts still gets a digest of
    the readable part, followed by ``+`` (used for a call's receiver, which has
    no preview to fall back on). Equal ``+`` digests mean the readable parts
    match; the unread parts are a stated gap.
    """
    return fingerprint_details(value, budget, partial_ok=partial_ok)[0]


def fingerprint_details(
    value: Any, budget: Budget | None = None, *, partial_ok: bool = False
) -> tuple[str, bool]:
    """Return the existing fingerprint and whether it can be reused for this object.

    Without an explicit budget the process's node ceiling applies (see ``configure``): a
    value over it is sketched, and the token ends with ``SKETCH_MARK``.
    """
    implicit = budget is None
    if implicit:
        budget = _effective()
    sketch = implicit and budget is not DEFAULT_BUDGET
    if CACHE and implicit:
        key = _cache_key(value)
        if key is not None:
            _current_caches()
            key = (key, partial_ok)
            found = _value_cache.get(key)
            if found is not None:
                return found
            normalized = STATS.tmp_normalized
            found = _fingerprint_details(value, budget, partial_ok, sketch=sketch)
            # A normalized temp path is counted for the gaps line on every
            # occurrence, so it is never answered from the cache.
            if STATS.tmp_normalized == normalized:
                if len(_value_cache) >= CACHE_LIMIT:
                    _value_cache.clear()
                _value_cache[key] = found
            return found
        if type(value) in (tuple, frozenset):
            # A deeply immutable value keeps its fingerprint between calls (see IDENTITY_LIMIT).
            _current_caches()
            entry = _identity_cache.get((id(value), partial_ok))
            if entry is not None and entry[0] is value:  # the same object, not a reused id
                return entry[1]
            normalized = STATS.tmp_normalized
            found = _fingerprint_details(value, budget, partial_ok, sketch=sketch)
            if (
                found[1] and found[0] not in STATUSES and STATS.last_visited >= IDENTITY_MIN_NODES
                and STATS.tmp_normalized == normalized
            ):
                if len(_identity_cache) >= IDENTITY_LIMIT:
                    _identity_cache.pop(next(iter(_identity_cache)))  # oldest first
                _identity_cache[id(value), partial_ok] = (value, found)
            return found
    return _fingerprint_details(value, budget, partial_ok, sketch=sketch)


def _fingerprint_details(
    value: Any, budget: Budget, partial_ok: bool, sketch: bool = False
) -> tuple[str, bool]:
    walk = None
    try:
        out = hashlib.blake2b(digest_size=DIGEST_BYTES)
        walk = _Walk(budget, partial_ok)
        walk.encode(value, out)
        STATS.last_visited = walk.visited
        return out.hexdigest() + (PARTIAL_MARK if walk.partial else ""), (
            walk.immutable and not walk.partial
        )
    except _Stop as stop:
        if (
            stop.status == UNKNOWN and sketch and walk is not None
            and budget.max_nodes is not None and walk.visited > budget.max_nodes
        ):
            try:
                return _sketch(value), False  # over the node ceiling: a sketch, never a guess
            except _Stop as inner:
                return inner.status, False
            except Exception:
                return FAILED, False
        return stop.status, False
    except RecursionError:
        return UNKNOWN, False
    except Exception:
        return FAILED, False
    finally:
        if walk is not None:
            STATS.nodes += walk.visited
            STATS.walks += 1
            STATS.walked = walk.visited
            STATS.walked_type = type(value)
