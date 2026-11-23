"""Behavioural comparison and ranked localisation."""

from __future__ import annotations

import difflib
import hashlib
import json

from .fingerprint import PARTIAL_MARK, REDACTED, SKETCH_MARK, is_digest
from .fingerprint import UNKNOWN as FP_UNKNOWN
from .node import Node

UNCHANGED = "unchanged"
ORIGIN = "origin"
INPUT_CHANGED = "input-changed"
DOWNSTREAM = "downstream"
ADDED = "added"
REMOVED = "removed"
UNSTABLE = "unstable"
RAISED = "raised"
HANDLED = "handled-exception"


UNKNOWN = "unknown"
EQUAL_READABLE = "equal-readable"

# Per-field comparison outcomes. A comparison answers equal, different or
# unknown; a redacted value is never compared, so it cannot make a call differ.
# `_PARTIAL` is a fourth, narrower answer: two digests that end in `+` match in
# everything LDR could read, but some part (a lock, a socket, a C object) was
# unreadable, so that is not proof of equality and is never reported as equal.
_EQUAL = "equal"
_DIFFERENT = "different"
_UNKNOWN = "unknown"
_PARTIAL = "equal-readable"


def identity(node: Node) -> str:
    """Checkout-independent identity; display paths and lines are diagnostic."""
    source = node.source_id or node.file
    return f"{source}:{node.name}" if source else node.name


def _compare_value(old_text, old_fp, new_text, new_fp) -> tuple[str, bool]:
    """Compare one captured value; also say if only the fingerprints differ.

    Fingerprints cover the full value; the text is a cut-off preview. When both
    sides have a digest the digest decides. Otherwise (older traces, types not
    read structurally) the preview text is all there is to compare.
    """
    if is_digest(old_fp) and is_digest(new_fp):
        if old_fp == new_fp:
            return (_PARTIAL if old_fp.endswith((PARTIAL_MARK, SKETCH_MARK)) else _EQUAL), False
        return _DIFFERENT, old_text == new_text
    if REDACTED in (old_fp, new_fp):
        return _EQUAL, False
    if FP_UNKNOWN in (old_fp, new_fp):
        return _UNKNOWN, False
    return (_EQUAL if old_text == new_text else _DIFFERENT), False


def _compare_fingerprints(old_fp, new_fp) -> tuple[str, bool]:
    """Compare values that only exist as fingerprints. Never reports a guess."""
    if is_digest(old_fp) and is_digest(new_fp):
        if old_fp != new_fp:
            return _DIFFERENT, False
        return (_PARTIAL if old_fp.endswith((PARTIAL_MARK, SKETCH_MARK)) else _EQUAL), False
    if FP_UNKNOWN in (old_fp, new_fp):
        return _UNKNOWN, False
    return _EQUAL, False  # absent or redacted on a side: not compared


def compare_fields(old: Node, new: Node) -> dict[str, tuple[str, bool]]:
    """Per-field outcome for a pair of calls: ``in.<name>``, ``out``, ``error``, ``handled``.

    Every field is compared, including the ones that varied between two runs of identical
    code: a change is never left out. Those get a note instead (see ``varied_fields``).
    """
    fields: dict[str, tuple[str, bool]] = {}
    for key in sorted(set(old.in_) | set(new.in_) | set(old.in_fp) | set(new.in_fp)):
        if key not in old.in_ and key not in new.in_:
            # Fingerprint only, such as a receiver (`self`): it has no preview.
            fields[f"in.{key}"] = _compare_fingerprints(old.in_fp.get(key), new.in_fp.get(key))
            continue
        if key not in old.in_ or key not in new.in_:
            fields[f"in.{key}"] = (_DIFFERENT, False)
            continue
        fields[f"in.{key}"] = _compare_value(
            old.in_[key], old.in_fp.get(key), new.in_[key], new.in_fp.get(key)
        )
    fields["out"] = _compare_value(old.out, old.out_fp, new.out, new.out_fp)
    fields["error"] = (_EQUAL if old.error == new.error else _DIFFERENT, False)
    fields["handled"] = (_EQUAL if old.handled == new.handled else _DIFFERENT, False)
    fields["input_changes"] = (
        _EQUAL if old.input_changes == new.input_changes else _DIFFERENT,
        False,
    )
    return fields


from .previous import VARIED_NOTE, VARIED_RUNS  # noqa: E402,F401


def varied_fields(old: Node, new: Node) -> list[str]:
    """Fields that differ now and also differed between two runs of identical code."""
    learned = set(old.volatile) | set(new.volatile)
    return [
        name for name, (state, _) in compare_fields(old, new).items()
        if state == _DIFFERENT and name in learned
    ]


def _own_state(old: Node, new: Node) -> str:
    states = {state for state, _ in compare_fields(old, new).values()}
    for state in (_DIFFERENT, _UNKNOWN, _PARTIAL):
        if state in states:
            return state
    return _EQUAL


def beyond_preview(old: Node, new: Node) -> list[str]:
    """Fields whose full values differ although their previews read the same."""
    return [
        name
        for name, (state, only_fingerprint) in compare_fields(old, new).items()
        if state == _DIFFERENT and (only_fingerprint or _fingerprint_only(old, new, name))
    ]


def _fingerprint_only(old: Node, new: Node, field: str) -> bool:
    key = field[3:] if field.startswith("in.") else None
    return key is not None and key not in old.in_ and key not in new.in_


def unknown_fields(old: Node, new: Node) -> list[str]:
    return [name for name, (state, _) in compare_fields(old, new).items() if state == _UNKNOWN]


def partial_fields(old: Node, new: Node) -> list[str]:
    return [name for name, (state, _) in compare_fields(old, new).items() if state == _PARTIAL]


def _inputs_differ(old: Node, new: Node) -> bool:
    return any(
        state == _DIFFERENT
        for name, (state, _) in compare_fields(old, new).items()
        if name.startswith("in.")
    )


def _fp_or_text(node: Node, key: str) -> str:
    fingerprint = node.in_fp.get(key)
    return fingerprint if is_digest(fingerprint) else "t:" + node.in_[key]


def _own(node: Node) -> dict:
    """What a call's hash covers: everything, including fields that varied between runs."""
    payload = {
        "name": node.name,
        "in": dict(node.in_),
        "out": node.out,
        "input_changes": node.input_changes,
        "error": node.error,
        "handled": node.handled,
    }
    if node.in_fp or node.out_fp:
        payload["in_fp"] = dict(node.in_fp)
        payload["out_fp"] = node.out_fp
    return payload


def shallow_hash(node: Node) -> str:
    payload = json.dumps(_own(node), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def node_hash(node: Node) -> str:
    payload = {
        "own": _own(node),
        "children": [node_hash(child) for child in node.children],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _input_token(node: Node) -> str:
    # Only for pairing calls: an input that varies between identical runs must not stop two
    # calls of the same function from being paired. It is still compared once they are.
    masks = set(node.volatile)
    values = {k: _fp_or_text(node, k) for k in node.in_ if f"in.{k}" not in masks}
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def align(old: list[Node], new: list[Node]):
    """Align calls using code identity and inputs, then pair edited blocks.

    Input-aware anchors prevent an inserted call to the same function from
    shifting every later call. Replace blocks are still paired by code
    identity so a genuine input edit remains ``input-changed``.
    """
    left = [(identity(node), _input_token(node)) for node in old]
    right = [(identity(node), _input_token(node)) for node in new]
    pairs: list[tuple[Node | None, Node | None]] = []
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend(zip(old[i1:i2], new[j1:j2]))
        elif tag == "delete":
            pairs.extend((node, None) for node in old[i1:i2])
        elif tag == "insert":
            pairs.extend((None, node) for node in new[j1:j2])
        else:
            old_block = old[i1:i2]
            new_block = new[j1:j2]
            count = max(len(old_block), len(new_block))
            for offset in range(count):
                left_node = old_block[offset] if offset < len(old_block) else None
                right_node = new_block[offset] if offset < len(new_block) else None
                pairs.append((left_node, right_node))
    return pairs


def mark_unstable(old: Node, noise: Node) -> None:
    """Learn volatile fields without suppressing deterministic descendants."""
    if identity(old) != identity(noise):
        return
    masks = set(old.volatile)
    # Same rule as comparison: full-value fingerprints decide, so a field that
    # varies only beyond its preview is learned too.
    for name, (state, _) in compare_fields(old, noise).items():
        if state == _DIFFERENT and name != "input_changes":
            masks.add(name)
    old.volatile = sorted(masks)
    old.unstable = bool(masks)
    for left, right in align(old.children, noise.children):
        if left is not None and right is not None:
            mark_unstable(left, right)


class Change:
    def __init__(self, kind, path, node, old, depth=0, leaf=True):
        self.varied: list[str] = []  # fields of this change that also varied with no edit
        self.kind = kind
        self.path = path
        self.node = node
        self.old = old
        self.depth = depth
        self.leaf = leaf

    @property
    def name(self) -> str:
        return self.node.name

    def __repr__(self):
        return f"<{self.kind} {self.path}>"


def _is_test(change: Change | str) -> bool:
    if isinstance(change, Change):
        file = change.node.file.replace("\\", "/").lower()
        if "/tests/" in f"/{file}" or file.rsplit("/", 1)[-1].startswith("test_"):
            return True
        path = change.path
    else:
        path = change
    leaf = path.split(" > ")[-1]
    parts = leaf.replace("<locals>", "").split(".")
    return any(
        part.startswith("test_") or part in ("setUp", "tearDown", "pretest", "posttest")
        for part in parts
        if part
    )


def rank(change: Change) -> tuple:
    kind_rank = {
        RAISED: 0,
        ORIGIN: 1,
        HANDLED: 2,
        ADDED: 3,
        REMOVED: 3,
    }.get(change.kind, 4)
    return (1 if _is_test(change) else 0, kind_rank, 0 if change.leaf else 1, -change.depth)


def diff_tree(
    old: Node | None, new: Node | None, path: str = "", depth: int = 0
) -> list[Change]:
    if old is None and new is None:
        return []
    if old is None:
        return [Change(ADDED, path + new.name, new, None, depth)]
    if new is None:
        return [Change(REMOVED, path + old.name, old, old, depth)]
    if identity(old) != identity(new):
        return [
            Change(REMOVED, path + old.name, old, old, depth),
            Change(ADDED, path + new.name, new, None, depth),
        ]

    here = path + new.name
    child_changes: list[Change] = []
    for left, right in align(old.children, new.children):
        child_changes.extend(diff_tree(left, right, here + " > ", depth + 1))

    own_state = _own_state(old, new)
    own_changed = own_state == _DIFFERENT
    unknown = (
        [Change(UNKNOWN, here, new, old, depth)]
        if own_state == _UNKNOWN
        else [Change(EQUAL_READABLE, here, new, old, depth)]
        if own_state == _PARTIAL
        else []
    )
    if not own_changed and not child_changes:
        return unknown

    from_below = [
        change
        for change in child_changes
        if change.kind in (ORIGIN, RAISED, HANDLED, DOWNSTREAM, ADDED, REMOVED)
    ]
    raised_now = old.error is None and new.error is not None
    deep_unhandled = any(child.error is not None and not child.handled for child in new.children)

    if raised_now and new.handled:
        kind = HANDLED
    elif raised_now and not deep_unhandled:
        kind = RAISED
    elif _inputs_differ(old, new) and own_changed:
        kind = INPUT_CHANGED
    elif from_below:
        kind = DOWNSTREAM
    elif own_changed:
        kind = ORIGIN
    else:
        kind = DOWNSTREAM

    self_change: list[Change] = []
    if own_changed:
        leaf = not any(
            change.kind in (RAISED, ORIGIN, HANDLED, ADDED, REMOVED)
            for change in child_changes
        )
        self_change = [Change(kind, here, new, old, depth, leaf)]
        self_change[0].varied = varied_fields(old, new)
    return self_change + unknown + child_changes


def unstable_fields(roots: list[Node]) -> list[dict[str, str]]:
    """Fields learned to vary between identical runs. They are compared like any other;
    a change in one carries a note."""
    found = []
    for root in roots:
        for node in root.walk():
            for name in sorted(set(node.volatile)):
                found.append(
                    {"call": node.frame_id or node.code_id, "function": node.name, "field": name}
                )
    return found


def render_unstable(items: list[dict[str, str]], limit: int = 10) -> str:
    if not items:
        return ""
    lines = ["VARIED BETWEEN IDENTICAL RUNS (still compared; a change in one carries a note)"]
    lines += [f"  {item['call']}  {item['field']}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"  (+{len(items) - limit} more in .ldr/last.json)")
    return "\n".join(lines)


def origins(changes: list[Change]) -> list[Change]:
    return [
        change
        for change in changes
        if change.kind in (RAISED, ORIGIN, HANDLED, ADDED, REMOVED)
    ]
