"""Token-bounded human and agent-readable trace rendering."""

from __future__ import annotations

from .diff import (
    ADDED,
    DOWNSTREAM,
    HANDLED,
    INPUT_CHANGED,
    ORIGIN,
    RAISED,
    REMOVED,
    UNSTABLE,
    Change,
)
from .node import Node

C = {
    "dim": "\033[90m",
    "fn": "\033[36m",
    "val": "\033[32m",
    "err": "\033[31m",
    "warn": "\033[33m",
    "off": "\033[0m",
}

MAX_ARGS = 6


def _sig(node: Node, color: bool) -> str:
    items = list(node.in_.items())
    cap = len(items) if node.error and not node.handled else MAX_ARGS
    extra = len(items) - cap
    arguments = ", ".join(f"{key}={value}" for key, value in items[:cap])
    if extra > 0:
        arguments += f", +{extra} more"
    name = f"{C['fn']}{node.name}{C['off']}" if color else node.name
    head = f"{name}({arguments})"
    if node.error and node.handled:
        tail = f" ~ handled {node.error}"
    elif node.error:
        tail = f" ! {C['err']}{node.error}{C['off']}" if color else f" ! {node.error}"
    elif node.out is not None:
        tail = f" -> {C['val']}{node.out}{C['off']}" if color else f" -> {node.out}"
    else:
        tail = ""
    location = f" @ {node.location}" if node.location else ""
    return head + tail + location


def _failing(node: Node) -> bool:
    return (node.error is not None and not node.handled) or any(
        _failing(child) for child in node.children
    )


def _collapse(children: list[Node]) -> list[tuple[Node, int]]:
    output: list[list] = []
    for child in children:
        key = (
            child.code_id,
            tuple(sorted(child.in_.items())),
            child.out,
            child.error,
            child.handled,
            len(child.children),
        )
        if output and output[-1][2] == key and not child.children:
            output[-1][1] += 1
        else:
            output.append([child, 1, key])
    return [(child, count) for child, count, _ in output]


def _informative(node: Node) -> bool:
    if node.error:
        return True
    if node.in_ or node.out not in (None, "None"):
        return True
    return any(_informative(child) for child in node.children)


def tree(
    node: Node,
    color: bool = False,
    depth: int = 0,
    max_depth: int = 99,
    prune: bool = False,
    count: int = 1,
    failure_only: bool = False,
) -> list[str]:
    if depth > max_depth:
        return []
    head = "  " * depth + _sig(node, color)
    if count > 1:
        head += f"   x{count}"
    output = [head]
    for line in node.lines:
        output.append("  " * (depth + 1) + line)
    children = node.children
    if failure_only:
        failing_children = [child for child in children if _failing(child)]
        context_children: list[Node] = []
        if node.error is not None and not node.handled:
            context_children = [
                child for child in children if not _failing(child) and _informative(child)
            ][-8:]
        selected = {id(child) for child in failing_children + context_children}
        children = [child for child in children if id(child) in selected]
    elif prune:
        children = [child for child in children if _informative(child)]
    for child, repetitions in _collapse(children):
        output.extend(
            tree(
                child,
                color,
                depth + 1,
                max_depth,
                prune,
                repetitions,
                failure_only and _failing(child),
            )
        )
    return output


def _bounded(lines: list[str], max_lines: int) -> list[str]:
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines
    if max_lines < 8:
        return lines[:max_lines]
    head = max(3, (max_lines * 2) // 3)
    tail = max_lines - head - 1
    omitted = len(lines) - head - tail
    return lines[:head] + [f"[... {omitted} rendered lines omitted ...]"] + lines[-tail:]


def render_run(
    roots: list[Node],
    color: bool = False,
    truncated: bool = False,
    full: bool = False,
    max_lines: int = 200,
    dropped_nodes: int = 0,
) -> str:
    blocks: list[str] = []
    failed = [root for root in roots if _failing(root)]
    if failed and not full:
        blocks.append("FAILED PATH")
        for root in failed:
            blocks.extend(tree(root, color, prune=True, failure_only=True))
        total = sum(root.count() for root in roots)
        shown = sum(root.count() for root in failed)
        if total > shown:
            blocks.extend(("", f"[{total - shown} recorded calls succeeded]"))
    else:
        blocks.append("FULL TRACE")
        top = roots if full else [root for root in roots if _informative(root)]
        for root, repetitions in _collapse(top):
            blocks.extend(tree(root, color, prune=not full, count=repetitions))
    if truncated:
        detail = (
            f"; {dropped_nodes} overflow path(s) switched to exception-only monitoring"
            if dropped_nodes
            else ""
        )
        blocks.extend(("", f"[trace budget reached{detail}; late exceptions remain visible]"))
    return "\n".join(_bounded(blocks, max_lines))


_LABEL = {
    RAISED: "RAISES HERE",
    HANDLED: "NEW HANDLED EXCEPTION",
    ORIGIN: "CHANGED HERE",
    INPUT_CHANGED: "different input",
    DOWNSTREAM: "downstream",
    ADDED: "new call",
    REMOVED: "call gone",
    UNSTABLE: "nondeterministic field ignored",
}


def render_call_chain(roots: list[Node], changes: list[Change], color: bool = False) -> str:
    by_node: dict[int, list[Change]] = {}
    for change in changes:
        by_node.setdefault(id(change.node), []).append(change)

    def walk(node: Node, depth: int) -> list[str]:
        mine = by_node.get(id(node), [])
        children: list[str] = []
        for child in node.children:
            children.extend(walk(child, depth + 1))
        if not mine and not children:
            return []
        tag = ""
        if mine:
            tag = "   <-- " + " / ".join(_LABEL[change.kind] for change in mine)
        return ["  " * depth + _sig(node, color) + tag] + children

    lines: list[str] = []
    for root in roots:
        lines.extend(walk(root, 0))
    return "\n".join(lines) if lines else "(changed call is absent from the current run)"


def _visible_inputs(old: Node, new: Node) -> tuple[dict[str, str], dict[str, str]]:
    masks = set(old.volatile) | set(new.volatile)
    left = {key: value for key, value in old.in_.items() if f"in.{key}" not in masks}
    right = {key: value for key, value in new.in_.items() if f"in.{key}" not in masks}
    return left, right


def _state(value: str | None) -> str:
    return "<no return>" if value is None else value


def render_diff(changes: list[Change], color: bool = False, max_findings: int = 25) -> str:
    if not changes:
        return "No behavioural change vs snapshot."
    from .diff import rank

    seen = set()
    unique = []
    for change in sorted(changes, key=rank):
        key = (change.kind, change.node.code_id, change.node.callsite)
        if key in seen:
            continue
        seen.add(key)
        unique.append(change)
    omitted = max(0, len(unique) - max_findings)
    unique = unique[:max_findings]

    lines = []
    for change in unique:
        location = f" @ {change.node.location}" if change.node.location else ""
        lines.append(f"[{_LABEL[change.kind]}] {change.path}{location}")
        if change.old is not None and change.kind != REMOVED:
            left_inputs, right_inputs = _visible_inputs(change.old, change.node)
            masks = set(change.old.volatile) | set(change.node.volatile)
            if left_inputs != right_inputs:
                lines.append(f"    in : {left_inputs} -> {right_inputs}")
            if "out" not in masks and change.old.out != change.node.out:
                lines.append(f"    out: {_state(change.old.out)} -> {_state(change.node.out)}")
            if "error" not in masks and change.old.error != change.node.error:
                lines.append(f"    err: {change.old.error} -> {change.node.error}")
    if omitted:
        lines.append(f"[{omitted} lower-ranked findings omitted]")
    return "\n".join(lines)
