"""Token-bounded human and agent-readable trace rendering."""

from __future__ import annotations

import shlex

from .diff import (
    ADDED,
    DOWNSTREAM,
    HANDLED,
    INPUT_CHANGED,
    ORIGIN,
    RAISED,
    REMOVED,
    UNKNOWN,
    UNSTABLE,
    Change,
    beyond_preview,
    unknown_fields,
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


def _compact_value(value: str, limit: int = 260) -> str:
    if len(value) <= limit:
        return value
    head = (limit * 2) // 3
    tail = limit - head - 23
    return value[:head] + "... <value omitted> ..." + value[-tail:]


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
            tuple(
                (name, change.get("before"), change.get("after"))
                for name, change in sorted(child.input_changes.items())
            ),
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
    for name, change in list(node.input_changes.items())[:3]:
        before = _compact_value(change.get("before", "<unavailable>"))
        after = _compact_value(change.get("after", "<unavailable>"))
        output.append(
            "  " * (depth + 1)
            + f"INPUT MUTATION {name}: {before} -> {after}"
        )
    if len(node.input_changes) > 3:
        output.append(
            "  " * (depth + 1)
            + f"[{len(node.input_changes) - 3} more mutated input(s) in JSON]"
        )
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


def _render_capture_limits(
    truncated: bool,
    dropped_nodes: int,
    sampled_nodes: int,
    max_nodes: int | None,
    recorded: int | None,
) -> list[str]:
    parts = [f"recorded={recorded}"]
    if max_nodes is not None:
        parts.append(f"max-nodes={max_nodes}")
    parts.extend(
        (
            f"truncated={truncated}",
            f"overflow={dropped_nodes}",
            f"sampled={sampled_nodes}",
        )
    )
    return ["", "CAPTURE LIMITS: " + " ".join(parts)]


def _inspection_command(target: str, scope: list[str] | None, frame: str) -> str:
    parts = ["ldr", "inspect", target, "--frame", frame]
    if scope:
        parts += ["--only", *dict.fromkeys(scope)]
    return " ".join(shlex.quote(str(part)) for part in parts)


def _render_suggested_inspections(suggestions: list[dict]) -> list[str]:
    lines: list[str] = ["", "SUGGESTED INSPECTIONS"]
    if not suggestions:
        lines.append("  No completed project frame could be ranked from this capture.")
        lines.append("  Review .ldr/last.json frame_index or correct the source scope.")
        return lines
    for position, suggestion in enumerate(suggestions[:3], 1):
        location = (
            f" @ {suggestion.get('location')}"
            if suggestion.get("location")
            else ""
        )
        lines.append(
            f"{position}. frame {suggestion.get('id')} — "
            f"{suggestion.get('function')}{location}"
        )
        for reason in suggestion.get("reasons", [])[:2]:
            lines.append(f"   Reason: {reason}")
        if suggestion.get("match_strategy") == "all_calls":
            calls = suggestion.get("evidence", {}).get("recorded_calls")
            lines.append(f"   Inspect: all {calls} recorded calls of this function")
        lines.append(f"   Run: ldr inspect --frame-id {suggestion.get('id')}")
    lines.append("  These are evidence-ranked follow-ups, not claimed bug locations.")
    return lines


def render_run(
    roots: list[Node],
    color: bool = False,
    truncated: bool = False,
    full: bool = False,
    max_lines: int = 200,
    dropped_nodes: int = 0,
    sampled_nodes: int = 0,
    *,
    frame_index: dict | None = None,
    suggestions: list[dict] | None = None,
    command_target: str | None = None,
    scope: list[str] | None = None,
    status: dict | None = None,
    max_nodes: int | None = None,
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
    if truncated and dropped_nodes:
        detail = (
            f"; {dropped_nodes} overflow path(s) switched to exception-only monitoring"
            if dropped_nodes
            else ""
        )
        blocks.extend(
            ("", f"[trace node budget reached{detail}; late exceptions remain visible]")
        )
    elif truncated and not sampled_nodes:
        blocks.extend(("", "[trace capture is incomplete; consult CAPTURE LIMITS]"))
    if sampled_nodes:
        blocks.extend((
            "",
            f"[{sampled_nodes} repetitive call(s) sampled; early and power-of-two calls retained]",
        ))
    if suggestions is not None:
        blocks.extend(_render_suggested_inspections(suggestions))
    if truncated or sampled_nodes:
        recorded = (
            status.get("recorded_nodes")
            if status is not None
            else sum(root.count() for root in roots)
        )
        blocks.extend(
            _render_capture_limits(
                truncated, dropped_nodes, sampled_nodes, max_nodes, recorded
            )
        )
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
    UNKNOWN: "cannot compare",
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
            for field in beyond_preview(change.old, change.node):
                label = field[3:] if field.startswith("in.") else field
                prefix = "in " if field.startswith("in.") else ""
                lines.append(
                    f"    {prefix}{label}: changed beyond the preview or in ordering "
                    "(the previews read the same)"
                )
            for field in unknown_fields(change.old, change.node):
                lines.append(f"    {field}: cannot be compared (value past the capture budget)")
    if omitted:
        lines.append(f"[{omitted} lower-ranked findings omitted]")
    return "\n".join(lines)


def render_inspection(
    nodes: list[Node],
    requested_frame: str,
    max_lines: int = 200,
    *,
    command_target: str | None = None,
    scope: list[str] | None = None,
    frame_index: dict | None = None,
) -> str:
    """Render the state history for one portable frame id."""
    if not nodes:
        lines = [
            "INSPECTION MISSED",
            f"Frame {requested_frame!r} was not observed on the re-run.",
            "The execution path or call ordering changed; run a fresh LDR trace and select a new frame id.",
            "",
            "AVAILABLE SELECTORS (from last broad run)",
        ]
        if frame_index:
            for fr in frame_index.get("frames", [])[:8]:
                lines.append(f"  {fr.get('frame_id')}")
            for func in frame_index.get("functions", [])[:5]:
                lines.append(f"  {func.get('inspect_selector')}  # {func.get('function')}")
        if command_target:
            lines.append("")
            lines.append(
                f"  Retry: {_inspection_command(command_target, scope, requested_frame)}"
            )
        lines.append("  JSON: .ldr/last.json frame_index")
        return "\n".join(_bounded(lines, max_lines))

    lines = [
        f"INSPECTED FRAMES: {len(nodes)} matched selector {requested_frame!r}"
        if len(nodes) > 1
        else "INSPECTED FRAME"
    ]
    for idx, node in enumerate(nodes, 1):
        if len(nodes) > 1:
            lines.append(f"--- [{idx}/{len(nodes)}] {node.frame_id} ---")
        else:
            lines.append(f"{node.frame_id}")
        lines.append(_sig(node, False))
        if frame_index:
            parent = next(
                (
                    frame.get("parent_id")
                    for frame in frame_index.get("frames", [])
                    if frame.get("frame_id") == node.frame_id
                ),
                None,
            )
            if parent:
                lines.append(f"  parent: {parent}")
        lines.append("")
        lines.append("CALLS IN FRAME")
        if not node.children:
            lines.append("(no traced Python calls in this frame)")
        for child in node.children[-30:]:
            lines.append(f"    {_sig(child, False)}  [{child.frame_id}]")
        if len(node.children) > 30:
            lines.append(f"[{len(node.children) - 30} earlier calls omitted]")
        lines.append("")
        lines.append("STATE TRANSITIONS")
        if not node.state_steps:
            lines.append("(no Python local-state transitions were captured)")
        meaningful_steps = []
        for step in node.state_steps:
            statement = (step.get("statement") or "").strip()
            control_flow = statement.startswith(
                (
                    "if ",
                    "elif ",
                    "else",
                    "for ",
                    "async for ",
                    "while ",
                    "match ",
                    "case ",
                    "return ",
                    "raise ",
                    "yield ",
                )
            )
            mutation = any(
                marker in statement
                for marker in (".add(", ".append(", ".extend(", ".put(", ".put_nowait(")
            )
            if step.get("changes") or control_flow or mutation:
                meaningful_steps.append(step)
        hidden_steps = len(node.state_steps) - len(meaningful_steps)
        for step in meaningful_steps:
            statement = step.get("statement") or "<source unavailable>"
            lines.append(f"{step.get('line', '?')} | {statement}")
            for change in step.get("changes", []):
                lines.append(
                    f"    {change.get('name')}: {change.get('before')} "
                    f"-> {change.get('after')}"
                )
        if hidden_steps:
            lines.append(f"[{hidden_steps} low-signal state step(s) hidden]")
        if node.state_truncated:
            lines.append("[state-step budget reached; remaining transitions omitted]")
        if command_target:
            base = node.frame_id.split("#")[0] if "#" in node.frame_id else node.frame_id
            wildcard = f"{base}#*"
            lines.extend(
                (
                    "",
                    "ALL RECORDED CALLS OF THIS FUNCTION",
                    f"  {_inspection_command(command_target, scope, wildcard)}",
                )
            )
    return "\n".join(_bounded(lines, max_lines))
