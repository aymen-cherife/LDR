"""Trace node schema and safe, bounded value capture."""

from __future__ import annotations

import re
import reprlib
from dataclasses import dataclass, field
from typing import Any

# Repr limits. These are the difference between a readable trace and a
# 40k-token wall. Tuned to keep a node under ~200 chars in the common case.
_repr = reprlib.Repr()
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


def _redact_text(rendered: str) -> str:
    rendered = _ADDR.sub("0x*", rendered)
    for pattern in _SECRET_VALUES:
        rendered = pattern.sub("<redacted>", rendered)
    return rendered


def is_secret_name(name: object) -> bool:
    return isinstance(name, str) and bool(_SECRET_NAME.search(name))


def _redact_value(value: Any, depth: int = 0, seen: set[int] | None = None) -> Any:
    """Redact built-in containers without invoking arbitrary container code."""
    if depth >= 8:
        return "<max-depth>"
    seen = set() if seen is None else seen
    if type(value) in (dict, tuple, list, set, frozenset):
        value_id = id(value)
        if value_id in seen:
            return "<cycle>"
        seen = seen | {value_id}
    if type(value) is dict:
        return {
            key: "<redacted>" if is_secret_name(key) else _redact_value(item, depth + 1, seen)
            for key, item in value.items()
        }
    if type(value) is tuple:
        return tuple(_redact_value(item, depth + 1, seen) for item in value)
    if type(value) is list:
        return [_redact_value(item, depth + 1, seen) for item in value]
    if type(value) is set:
        return {_redact_value(item, depth + 1, seen) for item in value}
    if type(value) is frozenset:
        return frozenset(_redact_value(item, depth + 1, seen) for item in value)
    return value


def capture(value: Any, name: str | None = None) -> str:
    """Render a value to a bounded string, never raising.

    Objects whose __repr__ blows up (half-initialised, DB handles,
    C extensions) are common in real code. A tracer that dies inside
    repr() is worse than no tracer.
    """
    if name is not None and is_secret_name(name):
        return "<redacted>"
    try:
        return _redact_text(_repr.repr(_redact_value(value)))
    except Exception:
        try:
            return f"<{type(value).__name__} repr-failed>"
        except Exception:
            return "<unrepresentable>"


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
    children: list["Node"] = field(default_factory=list)

    # Location and diagnostic bookkeeping are not timing behaviour.
    module: str = ""
    file: str = ""
    source_id: str = ""
    line: int = 0
    callsite: str = ""
    handled: bool = False
    volatile: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    unstable: bool = False

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
        }

    @staticmethod
    def from_dict(d: dict) -> "Node":
        n = Node(
            name=d["name"],
            in_=d.get("in", {}),
            out=d.get("out"),
            error=d.get("error"),
            ms=d.get("ms", 0.0),
            module=d.get("module", ""),
            file=d.get("file", ""),
            source_id=d.get("source_id", ""),
            line=d.get("line", 0),
            callsite=d.get("callsite", ""),
            handled=d.get("handled", False),
            volatile=list(d.get("volatile", [])),
        )
        n.lines = d.get("lines", [])
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
