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


def _frame_sig_line(node: Node) -> str:
    return _sig(node, False)


def _render_capture_limits(
    truncated: bool,
    dropped_nodes: int,
    sampled_nodes: int,
    max_nodes: int | None,
    max_calls_per_function: int | None,
    recorded: int | None,
    max_lines: int,
) -> list[str]:
    lines: list[str] = ["", "CAPTURE LIMITS"]
    if max_nodes is not None:
        lines.append(f"  trace budget: max-nodes={max_nodes} recorded={recorded} truncated={truncated}")
    else:
        lines.append(f"  trace truncated: {truncated}")
    if dropped_nodes:
        lines.append(f"  overflow: {dropped_nodes} path(s) switched to exception-only monitoring; late exceptions remain visible")
    else:
        lines.append("  overflow: 0")
    if max_calls_per_function is not None:
        lines.append(f"  sampling: max-calls-per-function={max_calls_per_function} sampled-repetitions={sampled_nodes}")
        if sampled_nodes:
            lines.append("  sampling detail: early calls 1-8 and power-of-two samples retained; others omitted")
    if max_lines is not None:
        lines.append(f"  rendering: max-lines={max_lines} (head/tail bounded)")
    return lines


def _render_invocation_index(frame_index: dict, max_entries: int = 60) -> list[str]:
    frames = frame_index.get("frames", [])
    functions = frame_index.get("functions", [])
    total = frame_index.get("total", len(frames))
    lines: list[str] = ["", "INVOCATION INDEX"]
    lines.append(f"  frames: {len(frames)} shown / {total} recorded (execution order, parent-linked)")
    for idx, fr in enumerate(frames[:max_entries], 1):
        parent = fr.get("parent_id") or "-"
        err = fr.get("error")
        err_tag = " ! error" if err and not fr.get("handled") else (" ~ handled" if err else "")
        loc = fr.get("location") or fr.get("source_id") or ""
        lines.append(f"  {idx:3d}. {fr.get('frame_id')}  parent={parent}  @ {loc}{err_tag}")
    if len(frames) > max_entries:
        lines.append(f"  [... {len(frames)-max_entries} more frames omitted; see .ldr/last.json frame_index.frames ...]")
    if frame_index.get("truncated"):
        lines.append("  [frame_index truncated; increase collection limit to see more]")
    lines.append("")
    lines.append("FUNCTION INDEX")
    for func in functions:
        samples = ", ".join(func.get("sample_frame_ids", [])[:3])
        if len(func.get("sample_frame_ids", [])) > 3:
            samples += ", ..."
        lines.append(
            f"  {func.get('source_id')}:{func.get('function')} @ {func.get('location')}  "
            f"calls={func.get('recorded_calls')} selector='{func.get('inspect_selector')}' samples=[{samples}]"
            + (f" errors={func.get('error_count')}" if func.get("error_count") else "")
        )
    return lines


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(c.isalnum() or c in "-_./:#" for c in value):
        return value
    # Prefer single quotes for frame selectors which contain colons and hashes.
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', '\\"') + '"'


def _inspection_command(target: str, scope: list[str] | None, frame: str) -> str:
    parts = ["ldr", "inspect", target, "--frame", _shell_quote(frame)]
    if scope:
        parts += ["--only", *scope]
    return " ".join(parts)


def _render_available_inspections(
    frame_index: dict,
    invocation: dict | None,
    scope: list[str] | None,
) -> list[str]:
    target = (invocation or {}).get("target") or "<target>"
    # Scope for command: prefer original invocation scope if available
    cmd_scope = scope or (invocation or {}).get("scope")
    # Normalize scope entries to display form without project-root prefix pollution
    if isinstance(cmd_scope, list):
        cmd_scope = [str(s) for s in cmd_scope]
    else:
        cmd_scope = None
    functions = frame_index.get("functions", [])
    frames = frame_index.get("frames", [])
    lines: list[str] = ["", "AVAILABLE INSPECTIONS"]
    lines.append("  Copy-paste one of these to inspect a single invocation or all invocations of a function.")
    # Show a few concrete frame ids (up to 8) plus wildcard selectors.
    for fr in frames[:8]:
        fid = fr.get("frame_id") or ""
        if not fid:
            continue
        lines.append(f"  single: {_inspection_command(target, cmd_scope, fid)}")
    if len(frames) > 8:
        lines.append(f"  [... {len(frames)-8} more single-frame selectors in JSON ...]")
    for func in functions[:8]:
        sel = func.get("inspect_selector")
        if sel:
            lines.append(f"  all-of-function: {_inspection_command(target, cmd_scope, sel)}  # {func.get('function')} x{func.get('recorded_calls')}")
    if len(functions) > 8:
        lines.append(f"  [... {len(functions)-8} more function selectors in JSON ...]")
    if not frames and not functions:
        lines.append("  (no traced frames)")
    lines.append("  JSON selectors: .ldr/last.json frame_index.frames[].frame_id  /  frame_index.functions[].inspect_selector")
    return lines


def _render_value_transitions(roots: list[Node], frame_index: dict) -> list[str]:
    # Group nodes by code_id to find distinct input/output patterns already captured.
    from collections import defaultdict

    groups: dict[str, list[Node]] = defaultdict(list)
    for root in roots:
        for node in root.walk():
            groups[node.code_id].append(node)
    lines: list[str] = ["", "VALUE TRANSITIONS"]
    lines.append("  Distinct input/output patterns already captured (factual). Rare patterns are preserved despite collapsing/sampling.")
    shown = 0
    for code_id, nodes in sorted(groups.items()):
        if len(nodes) <= 1:
            continue
        # Determine distinct (in, out, error) triples
        distinct: dict[tuple, int] = {}
        for n in nodes:
            key = (tuple(sorted(n.in_.items())), n.out, n.error)
            distinct[key] = distinct.get(key, 0) + 1
        if len(distinct) <= 1:
            # All identical – note collapsed but not a transition
            if len(nodes) > 2:
                lines.append(f"  {code_id}: {len(nodes)} calls, 1 pattern collapsed (identical), sampling retained {len(nodes)} of them")
                shown += 1
            continue
        lines.append(f"  {code_id}: {len(nodes)} calls, {len(distinct)} distinct patterns:")
        for (inputs, out, err), cnt in list(distinct.items())[:4]:
            in_txt = ", ".join(f"{k}={v}" for k, v in inputs) if inputs else "-"
            out_txt = f" -> {out}" if out is not None else ""
            err_txt = f" ! {err}" if err else ""
            lines.append(f"    - {{{in_txt}}}{out_txt}{err_txt}  x{cnt}")
        if len(distinct) > 4:
            lines.append(f"    [... {len(distinct)-4} more patterns omitted ...]")
        shown += 1
        if shown >= 6:
            break
    if shown == 0:
        lines.append("  (no repeated function with multiple distinct patterns in this trace; single-pattern repetitions were collapsed as xN)")
    return lines


def _render_event_sequence(roots: list[Node], limit: int = 40) -> list[str]:
    # Flat chronological order of entry (pre-order walk approximates start order).
    ordered: list[Node] = []
    for root in roots:
        ordered.extend(list(root.walk()))
    # Sort by first occurrence is already walk order; keep it.
    lines: list[str] = ["", "EVENT SEQUENCE"]
    lines.append(f"  Chronological entry order (depth indicates nesting at entry time): {len(ordered)} events")
    # Build depth map via tree walk
    def walk_with_depth(node: Node, depth: int):
        yield (node, depth)
        for child in node.children:
            yield from walk_with_depth(child, depth+1)
    seq: list[tuple[Node, int]] = []
    for root in roots:
        seq.extend(list(walk_with_depth(root, 0)))
    for idx, (node, depth) in enumerate(seq[:limit], 1):
        indent = "  " * depth
        lines.append(f"  {idx:3d}. {indent}{_sig(node, False)}  [{node.frame_id}]")
    if len(seq) > limit:
        lines.append(f"  [... {len(seq)-limit} more events omitted ...]")
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
    invocation: dict | None = None,
    scope: list[str] | None = None,
    status: dict | None = None,
    max_nodes: int | None = None,
    max_calls_per_function: int | None = None,
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
    if sampled_nodes:
        blocks.extend((
            "",
            f"[{sampled_nodes} repetitive call(s) sampled; early and power-of-two calls retained]",
        ))
    # Extended navigational sections (factual, neutral)
    if frame_index is not None:
        blocks.extend(_render_invocation_index(frame_index))
        blocks.extend(_render_event_sequence(roots))
        blocks.extend(_render_value_transitions(roots, frame_index))
        blocks.extend(_render_available_inspections(frame_index, invocation, scope))
    # Capture limits always if we have any budget info
    if any(v is not None for v in [max_nodes, max_calls_per_function, status]) or truncated or sampled_nodes:
        recorded = None
        if status is not None:
            recorded = status.get("recorded_nodes")
        elif roots:
            recorded = sum(r.count() for r in roots)
        blocks.extend(_render_capture_limits(truncated, dropped_nodes, sampled_nodes, max_nodes, max_calls_per_function, recorded, max_lines))
    if frame_index is not None or invocation is not None:
        blocks.extend(["", "STRUCTURED RESULT"])
        blocks.append("  text: .ldr/last.txt   JSON: .ldr/last.json  (frame_index, failures, status, invocation)")
        if invocation:
            blocks.append(f"  invocation: runner={invocation.get('runner')} target={invocation.get('target')} scope={invocation.get('scope')}")
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


def _render_input_mutations(node: Node) -> list[str]:
    args = set(node.in_.keys())
    if not args:
        return []
    mutations: list[str] = []
    for step in node.state_steps:
        for ch in step.get("changes", []):
            name = ch.get("name")
            if name in args and ch.get("before") != ch.get("after"):
                # arg mutated inside frame
                mutations.append(f"  {name}: {ch.get('before')} -> {ch.get('after')} at line {step.get('line')}: {step.get('statement')}")
    if not mutations:
        return []
    out = ["", "INPUT MUTATIONS"]
    out.append("  Arguments that changed value inside the frame (factual, object identity via repr):")
    out.extend(mutations[:10])
    if len(mutations) > 10:
        out.append(f"  [... {len(mutations)-10} more mutations omitted ...]")
    return out


def render_inspection(
    nodes: list[Node],
    requested_frame: str,
    max_lines: int = 200,
    *,
    invocation: dict | None = None,
    scope: list[str] | None = None,
    frame_index: dict | None = None,
    status: dict | None = None,
    max_state_steps: int | None = None,
) -> str:
    """Render the state history for one portable frame id."""
    if not nodes:
        lines = [
            f"INSPECTION MISSED",
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
        if invocation:
            target = invocation.get("target") or "<target>"
            sc = scope or invocation.get("scope")
            lines.append("")
            lines.append(f"  Example: {_inspection_command(target, sc, requested_frame)}")
        lines.append("  JSON: .ldr/last.json frame_index")
        return "\n".join(_bounded(lines, max_lines))

    lines: list[str] = []
    # Header with exact invocation count
    if len(nodes) > 1:
        lines.append(f"INSPECTED FRAMES: {len(nodes)} matched selector {requested_frame!r}")
    else:
        lines.append("INSPECTED FRAME")
    for idx, node in enumerate(nodes, 1):
        if len(nodes) > 1:
            lines.append(f"--- [{idx}/{len(nodes)}] {node.frame_id} ---")
        else:
            lines.append(f"{node.frame_id}")
        # Exact selected invocation details
        lines.append(_sig(node, False))
        if node.callsite:
            lines.append(f"  callsite: {node.callsite}")
        lines.append(f"  location: {node.location}  source_id: {node.source_id}  frame_id: {node.frame_id}")
        lines.append(f"  timing: {node.ms:.2f}ms  handled={node.handled}")
        # Parent context if available via frame_index or callsite
        if frame_index:
            # find parent for this frame
            parent = next((f.get("parent_id") for f in frame_index.get("frames", []) if f.get("frame_id")==node.frame_id), None)
            if parent:
                lines.append(f"  parent: {parent}")
        lines.append("")
        lines.append("CALLS IN FRAME (chronological, within this invocation)")
        if not node.children:
            lines.append("(no traced Python calls in this frame)")
        else:
            for child in node.children[-30:]:
                lines.append(f"    {_sig(child, False)}  [{child.frame_id}]")
            if len(node.children) > 30:
                lines.append(f"[{len(node.children) - 30} earlier calls omitted]")
        lines.append("")
        lines.append("STATE TRANSITIONS (chronological, line-by-line local changes)")
        if not node.state_steps:
            lines.append("(no Python local-state transitions were captured; frame had no local assignments or max-state-steps filtered)")
        else:
            for step in node.state_steps:
                statement = step.get("statement") or "<source unavailable>"
                lines.append(f"{step.get('line', '?')} | {statement}")
                for change in step.get("changes", []):
                    lines.append(
                        f"    {change.get('name')}: {change.get('before')} -> {change.get('after')}"
                    )
            if node.state_truncated:
                lines.append("[state-step budget reached; remaining transitions omitted]")
        # Input mutations factual
        lines.extend(_render_input_mutations(node))
        lines.append("")
        # Coverage / limits for this focused run
        lines.append("CAPTURE LIMITS (focused)")
        ms = f"max-state-steps={max_state_steps}" if max_state_steps is not None else "state budget unknown"
        trunc = f"truncated={node.state_truncated}"
        calls = f"child-calls={len(node.children)}"
        lines.append(f"  {ms}  steps-recorded={len(node.state_steps)}  {trunc}  {calls}")
        if status:
            lines.append(f"  trace: recorded={status.get('recorded_nodes')} overflow={status.get('overflow_transitions')} sampled={status.get('sampled_repetitions')}")
        lines.append("")
        # Structured equivalent pointer
        lines.append("STRUCTURED RESULT")
        lines.append(f"  JSON: .ldr/last.json inspection.frames[{idx-1}]  (in, out, error, state_steps, children, frame_id)")
        lines.append(f"  JSON frame_id: inspection.frames[{idx-1}].frame_id == {node.frame_id!r}")
        if invocation:
            target = invocation.get("target") or "<target>"
            sc = scope or invocation.get("scope")
            lines.append(f"  rerun single: {_inspection_command(target, sc, node.frame_id)}")
            # wildcard alternative
            base = node.frame_id.split("#")[0] if "#" in node.frame_id else node.frame_id
            wildcard = f"{base}#*"
            lines.append(f"  rerun all of function: {_inspection_command(target, sc, wildcard)}")
        lines.append("")
    # Global available inspections from frame_index for navigation after focused
    if frame_index and len(nodes)==1:
        # show sibling frames of same function or other functions
        func_selectors = [f.get("inspect_selector") for f in frame_index.get("functions", []) if f.get("inspect_selector") and f.get("inspect_selector") != requested_frame]
        if func_selectors:
            lines.append("OTHER AVAILABLE INSPECTIONS (from last broad run)")
            for sel in func_selectors[:6]:
                tgt = (invocation or {}).get("target") or "<target>"
                sc = scope or (invocation or {}).get("scope")
                lines.append(f"  {_inspection_command(tgt, sc, sel)}")
            lines.append("  JSON: .ldr/last.json frame_index.functions[].inspect_selector")
    return "\n".join(_bounded(lines, max_lines))
