"""Trace node schema and safe, bounded value capture."""

from __future__ import annotations

import abc
import array
import collections
import datetime
import decimal
import re
import reprlib
import types
from dataclasses import dataclass, field
from typing import Any

from .structure import instance_dict, read, type_label

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


def _ordered(items: list, key=lambda item: item) -> list:
    """Sort like reprlib does, but never call user comparison code."""
    if all(_plain_sortable(key(item)) for item in items):
        try:
            return sorted(items, key=key)
        except Exception:
            return items
    return items


class _StructuralRepr(reprlib.Repr):
    """reprlib's bounded previews, without ever running a user object's code.

    Built-in types read exactly as ``reprlib`` renders them. Any other object
    is rendered from its stored state as ``ClassName(field=preview, ...)``.
    """

    def repr1(self, x, level):
        kind = type(x)
        if kind in _REPR_DISPATCH:
            return super().repr1(x, level)
        if kind in _PLAIN_REPR:
            return self.repr_instance(x, level)
        return self.repr_object(x, level)

    def repr_set(self, x, level):
        if not x:
            return "set()"
        return self._repr_iterable(_ordered(list(x)), level, "{", "}", self.maxset)

    def repr_frozenset(self, x, level):
        if not x:
            return "frozenset()"
        return self._repr_iterable(
            _ordered(list(x)), level, "frozenset({", "})", self.maxfrozenset
        )

    def repr_dict(self, x, level):
        n = len(x)
        if n == 0:
            return "{}"
        if level <= 0:
            return "{" + self.fillvalue + "}"
        pairs = _ordered(list(x.items()), key=lambda pair: pair[0])
        pieces = []
        for key, item in pairs[: self.maxdict]:
            shown = "'<redacted>'" if is_secret_name(key) else self.repr1(item, level - 1)
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
                piece = f"{name}=<redacted>"
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

# A focused inspection is already bounded by max_state_steps, so it can afford
# enough value detail to distinguish nearby runtime states.  This remains
# reprlib-bounded and uses the same redaction path as broad capture.
_state_repr = _StructuralRepr()
_state_repr.maxstring = 320
_state_repr.maxother = 500
_state_repr.maxlist = 10
_state_repr.maxtuple = 10
_state_repr.maxdict = 10
_state_repr.maxset = 10
_state_repr.maxarray = 10
_state_repr.maxlevel = 4


# Default object.__repr__ embeds the memory address. Across two processes
# that address is always different, so on real code EVERY object-valued
# argument reads as a behaviour change. Found the hard way on tqdm: the
# real regression was buried under a dozen address-only diffs.
_ADDR = re.compile(r"(?<= at )0x[0-9a-fA-F]{6,16}(?=>)")
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:password|passwd|secret|api_?key|access_?token|refresh_?token|"
    r"authorization|cookie|session_?id|private_?key)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUES = (
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"),
)
_STATE_SIGNAL = re.compile(r"(?:visible|layout|enabled|active|valid|closed|dirty)$")


def _redact_text(rendered: str) -> str:
    rendered = _ADDR.sub("0x*", rendered)
    for pattern in _SECRET_VALUES:
        rendered = pattern.sub("<redacted>", rendered)
    return rendered


def is_secret_name(name: object) -> bool:
    return isinstance(name, str) and bool(_SECRET_NAME.search(name))


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
        return "<redacted>"
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


def capture_state(value: Any, name: str | None = None) -> str:
    """Capture a focused local plus bounded, side-effect-free boolean object state."""
    if name is not None and is_secret_name(name):
        return "<redacted>"
    try:
        rendered = _redact_text(_state_repr.repr(value))
    except Exception:
        rendered = capture(value, name)
    attributes = instance_dict(value)
    if attributes is None:
        return rendered

    state: list[tuple[str, bool]] = []
    nested: list[tuple[str, bool]] = []
    for key, item in attributes.items():
        if is_secret_name(key):
            continue
        normalized = key.lstrip("_").lower()
        if type(item) is bool and (_STATE_SIGNAL.search(normalized) or len(state) < 4):
            state.append((key, item))
        if len(nested) >= 128:
            continue
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
    selected = state[:8] + nested[:8]
    if not selected:
        return rendered
    detail = ", ".join(f"{key}={item}" for key, item in selected)
    return f"{rendered} {{{detail}}}"


def capture_error(error: BaseException) -> str:
    """Bound exception text while keeping pytest's useful assertion detail."""
    try:
        detail = _redact_text(str(error))
    except Exception:
        detail = "<message unavailable>"
    if len(detail) > 600:
        detail = detail[:450] + f"... <{len(detail) - 550} chars omitted> ..." + detail[-100:]
    return f"{type(error).__name__}: {detail}"


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

    @property
    def code_id(self) -> str:
        return f"{self.source_id or self.file}:{self.line}:{self.name}"

    @property
    def location(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "in": self.in_,
            "out": self.out,
            "in_fp": self.in_fp,
            "out_fp": self.out_fp,
            "input_changes": self.input_changes,
            "error": self.error,
            "ms": round(self.ms, 2),
            "module": self.module,
            "file": self.file,
            "source_id": self.source_id,
            "line": self.line,
            "callsite": self.callsite,
            "handled": self.handled,
            "volatile": sorted(set(self.volatile)),
            "lines": self.lines,
            "unstable": self.unstable,
            "children": [c.to_dict() for c in self.children],
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
