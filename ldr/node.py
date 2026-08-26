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

# A focused inspection is already bounded by max_state_steps, so it can afford
# enough value detail to distinguish nearby runtime states.  This remains
# reprlib-bounded and uses the same redaction path as broad capture.
_state_repr = reprlib.Repr()
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
    # Preserve exact numeric representations for Decimal and other precise numerics.
    # reprlib truncates at maxother=80; a Decimal with 50 digits must not lose precision.
    try:
        type_name = type(value).__name__
        type_mod = type(value).__module__
        if type_name == "Decimal" and type_mod == "decimal":
            return _redact_text(repr(value))
        if isinstance(value, (int, float, complex)) and not isinstance(value, bool):
            direct = repr(value)
            if len(direct) < 500:
                return _redact_text(direct)
    except Exception:
        pass
    try:
        return _redact_text(_repr.repr(_redact_value(value)))
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
        rendered = _redact_text(_state_repr.repr(_redact_value(value)))
    except Exception:
        rendered = capture(value, name)
    try:
        attributes = vars(value)
    except Exception:
        return rendered
    if type(attributes) is not dict:
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
        try:
            child_attributes = vars(item)
        except Exception:
            continue
        if type(child_attributes) is not dict:
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
    children: list["Node"] = field(default_factory=list)

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
    def from_dict(d: dict) -> "Node":
        n = Node(
            name=d["name"],
            in_=d.get("in", {}),
            out=d.get("out"),
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
