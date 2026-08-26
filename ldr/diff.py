"""Behavioural comparison and ranked localisation."""

from __future__ import annotations

import difflib
import hashlib
import json

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


def identity(node: Node) -> str:
    """Checkout-independent identity; display paths and lines are diagnostic."""
    source = node.source_id or node.file
    return f"{source}:{node.name}" if source else node.name


def _masked_own(node: Node, masks: set[str] | None = None) -> dict:
    masks = set(node.volatile) | (masks or set())
    inputs = {
        key: value
        for key, value in node.in_.items()
        if f"in.{key}" not in masks
    }
    return {
        "name": node.name,
        "in": inputs,
        "out": None if "out" in masks else node.out,
        "input_changes": node.input_changes,
        "error": None if "error" in masks else node.error,
        "handled": None if "handled" in masks else node.handled,
    }


def shallow_hash(node: Node) -> str:
    payload = json.dumps(_masked_own(node), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def node_hash(node: Node) -> str:
    payload = {
        "own": _masked_own(node),
        "children": [node_hash(child) for child in node.children],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _own_equal(old: Node, new: Node) -> bool:
    masks = set(old.volatile) | set(new.volatile)
    return _masked_own(old, masks) == _masked_own(new, masks)


def _input_token(node: Node) -> str:
    masks = set(node.volatile)
    values = {k: v for k, v in node.in_.items() if f"in.{k}" not in masks}
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


def mark_unstable(old: Node, probe: Node) -> None:
    """Learn volatile fields without suppressing deterministic descendants."""
    if identity(old) != identity(probe):
        return
    masks = set(old.volatile)
    for key in set(old.in_) | set(probe.in_):
        if old.in_.get(key) != probe.in_.get(key):
            masks.add(f"in.{key}")
    if old.out != probe.out:
        masks.add("out")
    if old.error != probe.error:
        masks.add("error")
    if old.handled != probe.handled:
        masks.add("handled")
    old.volatile = sorted(masks)
    old.unstable = bool(masks)
    for left, right in align(old.children, probe.children):
        if left is not None and right is not None:
            mark_unstable(left, right)


class Change:
    def __init__(self, kind, path, node, old, depth=0, leaf=True):
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

    own_changed = not _own_equal(old, new)
    if not own_changed and not child_changes:
        return []

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
    elif old.in_ != new.in_ and own_changed:
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
    return self_change + child_changes


def origins(changes: list[Change]) -> list[Change]:
    return [
        change
        for change in changes
        if change.kind in (RAISED, ORIGIN, HANDLED, ADDED, REMOVED)
    ]
