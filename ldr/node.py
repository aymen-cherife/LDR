"""Trace node schema and safe, bounded value capture."""

from __future__ import annotations

import abc
import array
import collections
import datetime
import decimal
import itertools
import re
import reprlib
import types
from dataclasses import dataclass, field
from typing import Any

from .redact import by_name_placeholder, is_secret_name, redact
from .structure import instance_dict, read, type_label

__all__ = ["Node", "capture", "capture_error", "capture_state", "is_secret_name"]

_PLAIN_SCALARS = (str, int, float, bool, bytes, type(None))
# Exact types whose built-in repr is C code that cannot call user code.
_PLAIN_REPR = (
    type(None), bool, float, complex, bytes, bytearray, decimal.Decimal, range, slice,
    type(Ellipsis), type(NotImplemented), types.FunctionType, types.BuiltinFunctionType,
    datetime.datetime, datetime.date, datetime.time, datetime.timedelta,
    type, abc.ABCMeta,
)
_REPR_DISPATCH = (tuple, list, set, frozenset, dict, str, int, collections.deque, array.array)
_OBJECT_PREVIEW_CHARS = 400


def _plain_sortable(value, depth: int = 0) -> bool:
    kind = type(value)
    if kind in _PLAIN_SCALARS:
        return True
    return kind is tuple and depth < 3 and all(_plain_sortable(v, depth + 1) for v in value)


# reprlib sorts a whole set or dict to show its first few items. Past this size
# that is a full traversal for a six-item preview, so larger ones keep their
# own iteration order.
_SORT_LIMIT = 1000


def _ordered(items, key=lambda item: item, limit: int = 0) -> list:
    """Sort like reprlib does, but never call user comparison code.

    Returns at most ``limit`` leading items (all of them when ``limit`` is 0).
    """
    if len(items) > _SORT_LIMIT:
        return list(itertools.islice(items, limit or None))
    items = list(items)
    if all(_plain_sortable(key(item)) for item in items):
        try:
            items = sorted(items, key=key)
        except Exception:
            pass
    return items[:limit] if limit else items


class _StructuralRepr(reprlib.Repr):
    """reprlib's bounded previews, without ever running a user object's code.

    Built-in types read exactly as ``reprlib`` renders them. Any other object
    is rendered from its stored state as ``ClassName(field=preview, ...)``.
    """

    def repr1(self, x, level):
        kind = type(x)
        if kind in (bytes, bytearray):
            # Look for secrets in the whole value; the preview cuts it afterwards.
            cleaned, found = redact(bytes(x).decode("latin-1"))
            if found:
                return self.repr_instance(cleaned.encode("latin-1", "replace"), level)
        if kind in _REPR_DISPATCH:
            return super().repr1(x, level)
        if kind in _PLAIN_REPR:
            return self.repr_instance(x, level)
        return self.repr_object(x, level)

    def repr_str(self, x, level):
        # Redact the whole string first. Doing it after truncation would leave
        # a fragment of a secret that straddles the cut.
        return super().repr_str(redact(x)[0], level)

    def _render_head(self, x, level, left, right, maxiter):
        n = len(x)
        if level <= 0:
            return f"{left}{self.fillvalue}{right}"
        pieces = [self.repr1(item, level - 1) for item in _ordered(x, limit=maxiter)]
        if n > maxiter:
            pieces.append(self.fillvalue)
        return f"{left}{self._join(pieces, level)}{right}"

    def repr_set(self, x, level):
        if not x:
            return "set()"
        return self._render_head(x, level, "{", "}", self.maxset)

    def repr_frozenset(self, x, level):
        if not x:
            return "frozenset()"
        return self._render_head(x, level, "frozenset({", "})", self.maxfrozenset)

    def repr_dict(self, x, level):
        n = len(x)
        if n == 0:
            return "{}"
        if level <= 0:
            return "{" + self.fillvalue + "}"
        pairs = _ordered(x.items(), key=lambda pair: pair[0], limit=self.maxdict)
        pieces = []
        for key, item in pairs:
            shown = (
                repr(by_name_placeholder(item))
                if is_secret_name(key)
                else self.repr1(item, level - 1)
            )
            pieces.append(f"{self.repr1(key, level - 1)}: {shown}")
        if n > self.maxdict:
            pieces.append(self.fillvalue)
        return "{%s}" % self._join(pieces, level)

    def repr_object(self, x, level):
        if type(x) is types.MethodType:
            return "<bound method>"
        reading = read(x)
        if reading is None:
            return f"<{type_label(type(x))}>"
        label = reading.label
        if not reading.fields:
            return f"{label}()"
        if level <= 0:
            return f"{label}({self.fillvalue})"
        pieces: list[str] = []
        used = 0
        for name, item in reading.fields:
            if name is None:
                piece = self.repr1(item, level - 1)
            elif is_secret_name(name):
                piece = f"{name}={by_name_placeholder(item)}"
            else:
                piece = f"{name}={self.repr1(item, level - 1)}"
            if pieces and used + len(piece) > _OBJECT_PREVIEW_CHARS:
                break
            pieces.append(piece)
            used += len(piece) + 2
        hidden = len(reading.fields) - len(pieces)
        if hidden:
            pieces.append(f"(+{hidden} more fields)")
        return f"{label}({', '.join(pieces)})"


# Repr limits. These are the difference between a readable trace and a
# 40k-token wall. Tuned to keep a node under ~200 chars in the common case.
_repr = _StructuralRepr()
_repr.maxstring = 80
_repr.maxother = 80
_repr.maxlist = 6
_repr.maxtuple = 6
_repr.maxdict = 6
_repr.maxset = 6
_repr.maxarray = 6
_repr.maxlevel = 3

# Default object.__repr__ embeds the memory address. Across two processes
# that address is always different, so on real code EVERY object-valued
# argument reads as a behaviour change. Found the hard way on tqdm: the
# real regression was buried under a dozen address-only diffs.
_ADDR = re.compile(r"(?<= at )0x[0-9a-fA-F]{6,16}(?=>)")


def _redact_text(rendered: str) -> str:
    """Last line of defence on finished text: addresses and any detected secret."""
    return redact(_ADDR.sub("0x*", rendered))[0]


def redact_source_line(source: str) -> str:
    """Redact source text that itself contains a credential-shaped name."""
    if any(is_secret_name(name) for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source)):
        return "<redacted source line>"
    return _redact_text(source)


def capture(value: Any, name: str | None = None) -> str:
    """Render a value to a bounded string, never raising.

    Objects whose __repr__ blows up (half-initialised, DB handles,
    C extensions) are common in real code. A tracer that dies inside
    repr() is worse than no tracer.
    """
    if name is not None and is_secret_name(name):
        return by_name_placeholder(value)
    # Preserve exact numeric representations for Decimal and other precise numerics.
    # reprlib truncates at maxother=80; a Decimal with 50 digits must not lose precision.
    # Exact types only: a subclass may define its own __repr__.
    try:
        kind = type(value)
        if kind is decimal.Decimal:
            return _redact_text(repr(value))
        if kind in (int, float, complex):
            direct = repr(value)
            if len(direct) < 500:
                return _redact_text(direct)
    except Exception:
        pass
    try:
        return _redact_text(_repr.repr(value))
    except Exception:
        try:
            return f"<{type(value).__name__} repr-failed>"
        except Exception:
            return "<unrepresentable>"


def _state_value(value):
    """Render focused state with an explicit stack and a shared text buffer."""
    pieces = []
    active = set()
    stack = [iter(((value, pieces),))]
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
        else:
            stack.append(_state_parts(*child, active))
    return "".join(pieces)


def _state_parts(value, pieces, active):
    kind = type(value)
    if kind is str:
        pieces.append(repr(redact(value)[0]))
        return
    if kind is bytes or kind is bytearray:
        clean, found = redact(bytes(value).decode("latin-1"))
        pieces.append(repr(clean) if found else repr(value))
        return
    if kind is int:
        try:
            pieces.append(repr(value))
        except ValueError:
            pieces.append(hex(value))
        return
    if any(kind is plain for plain in _PLAIN_REPR):
        pieces.append(repr(value))
        return
    if kind is types.MethodType:
        pieces.append("<bound method>")
        return
    marker = id(value)
    if marker in active:
        pieces.append("<cycle>")
        return
    active.add(marker)
    try:
        if kind is dict:
            pieces.append("{")
            for index, (key, item) in enumerate(value.items()):
                if index:
                    pieces.append(", ")
                yield key, pieces
                pieces.append(": ")
                if is_secret_name(key):
                    pieces.append(repr(by_name_placeholder(item)))
                else:
                    yield item, pieces
            pieces.append("}")
        elif kind is set or kind is frozenset:
            parts = []
            for item in value:
                part = []
                yield item, part
                parts.append("".join(part))
            items = ", ".join(sorted(parts))
            if kind is set:
                pieces.append("{" + items + "}" if parts else "set()")
            else:
                pieces.append("frozenset({" + items + "})" if parts else "frozenset()")
        elif kind is tuple or kind is list or kind is collections.deque or kind is array.array:
            if kind is list:
                prefix, suffix = "[", "]"
            elif kind is tuple:
                prefix, suffix = "(", ",)" if len(value) == 1 else ")"
            elif kind is array.array:
                prefix, suffix = f"array({value.typecode!r}, [", "])"
            else:
                prefix, suffix = "deque([", "])"
            pieces.append(prefix)
            for index, item in enumerate(value):
                if index:
                    pieces.append(", ")
                yield item, pieces
            pieces.append(suffix)
        else:
            reading = read(value)
            if reading is None:
                pieces.append(f"<{type_label(kind)}>")
                return
            pieces.append(reading.label + "(")
            for index, (name, item) in enumerate(reading.fields):
                if index:
                    pieces.append(", ")
                if name is not None:
                    pieces.append(name + "=")
                if name is not None and is_secret_name(name):
                    pieces.append(by_name_placeholder(item))
                else:
                    yield item, pieces
            pieces.append(")")
    finally:
        active.remove(marker)


def capture_state(value: Any, name: str | None = None) -> str:
    """Capture a focused local and its side-effect-free boolean object state."""
    if name is not None and is_secret_name(name):
        return by_name_placeholder(value)
    try:
        rendered = _redact_text(_state_value(value))
    except Exception:
        rendered = f"<{type_label(type(value))} state unavailable>"
    attributes = instance_dict(value)
    if attributes is None:
        return rendered

    state: list[tuple[str, bool]] = []
    nested: list[tuple[str, bool]] = []
    for key, item in attributes.items():
        if is_secret_name(key):
            continue
        if type(item) is bool:
            state.append((key, item))
        child_attributes = instance_dict(item)
        if child_attributes is None:
            continue
        for child_key in ("_visible", "_in_layout", "visible", "in_layout"):
            child_value = child_attributes.get(child_key)
            if type(child_value) is bool:
                nested.append((f"{key}.{child_key}", child_value))

    nested.sort(
        key=lambda item: (
            item[1] is not False,
            not any(token in item[0].lower() for token in ("label", "text")),
            item[0],
        )
    )
    selected = state + nested
    if not selected:
        return rendered
    detail = ", ".join(f"{key}={item}" for key, item in selected)
    return f"{rendered} {{{detail}}}"


def count_redacted(roots: list[Node]) -> int:
    """How many recorded values were replaced by a secret placeholder (not compared)."""
    total = 0
    for root in roots:
        for node in root.walk():
            total += sum(token == "redacted" for token in node.in_fp.values())
            total += node.out_fp == "redacted"
    return total


def capture_error(error: BaseException) -> str:
    """Keep the redacted exception text, even when it is too long to print."""
    try:
        detail = _redact_text(str(error))
    except Exception:
        detail = "<message unavailable>"
    try:
        name = type(error).__name__
    except Exception:
        name = "Exception"
    return f"{name}: {detail}"


@dataclass
class Node:
    name: str
    in_: dict[str, str] = field(default_factory=dict)
    out: str | None = None
    error: str | None = None
    ms: float = 0.0
    children: list[Node] = field(default_factory=list)

    # A portable invocation identity. Unlike CPython's frame id this is stable
    # across an immediate re-run, so an agent can select one observed call for
    # a focused inspection pass.
    frame_id: str = ""

    # Location and diagnostic bookkeeping are not timing behaviour.
    module: str = ""
    file: str = ""
    source_id: str = ""
    line: int = 0
    callsite: str = ""
    handled: bool = False
    volatile: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    state_steps: list[dict[str, Any]] = field(default_factory=list)
    state_truncated: bool = False
    unstable: bool = False
    # Caller-visible changes to mutable arguments that retained object identity
    # throughout the call. Values contain bounded entry/exit evidence.
    input_changes: dict[str, dict[str, str]] = field(default_factory=dict)
    # Fingerprints of the full values (see fingerprint.py). The text fields above
    # are previews for reading; comparison uses these when both sides have them.
    in_fp: dict[str, str] = field(default_factory=dict)
    out_fp: str | None = None
    timing: dict[str, float | bool] | None = None

    @property
    def code_id(self) -> str:
        # The definition line is metadata, not identity: it moves when the file is edited.
        return f"{self.source_id or self.file}:{self.name}"

    @property
    def location(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_dict(self, *, include_children: bool = True) -> dict:
        return {
            "name": self.name,
            "in": self.in_,
            "out": self.out,
            "in_fp": self.in_fp,
            "out_fp": self.out_fp,
            "input_changes": self.input_changes,
            "error": self.error,
            "ms": round(self.ms, 2),
            **({"timing": self.timing} if self.timing is not None else {}),
            "module": self.module,
            "file": self.file,
            "source_id": self.source_id,
            "line": self.line,
            "callsite": self.callsite,
            "handled": self.handled,
            "volatile": sorted(set(self.volatile)),
            "lines": self.lines,
            "unstable": self.unstable,
            "children": [c.to_dict() for c in self.children] if include_children else [],
            "frame_id": self.frame_id,
            "state_steps": self.state_steps,
            "state_truncated": self.state_truncated,
        }

    @staticmethod
    def from_dict(d: dict) -> Node:
        n = Node(
            name=d["name"],
            in_=d.get("in", {}),
            out=d.get("out"),
            in_fp=dict(d.get("in_fp", {})),
            out_fp=d.get("out_fp"),
            input_changes=dict(d.get("input_changes", {})),
            error=d.get("error"),
            ms=d.get("ms", 0.0),
            timing=d.get("timing"),
            module=d.get("module", ""),
            file=d.get("file", ""),
            source_id=d.get("source_id", ""),
            line=d.get("line", 0),
            callsite=d.get("callsite", ""),
            handled=d.get("handled", False),
            volatile=list(d.get("volatile", [])),
            frame_id=d.get("frame_id", ""),
        )
        n.lines = d.get("lines", [])
        n.state_steps = list(d.get("state_steps", []))
        n.state_truncated = bool(d.get("state_truncated", False))
        n.unstable = d.get("unstable", False)
        # v0.3 marked the whole node unstable. Preserve compatibility but
        # convert it to field masks so descendants remain comparable.
        if n.unstable and not n.volatile:
            n.volatile = ["out", "error", "handled", *[f"in.{k}" for k in n.in_]]
        n.children = [Node.from_dict(c) for c in d.get("children", [])]
        return n

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def count(self) -> int:
        return sum(1 for _ in self.walk())
