"""Evidence-based follow-up ranking for agent investigations.

The ranker deliberately works with one captured execution.  It therefore does
not present statistical suspiciousness or causal claims.  It combines signals
that are observable in that execution: location on a failing branch, completion
proximity, in-place input mutation, repetition, and common diagnostic noise.
Words shared between a function's name and the error text are NOT a signal:
on a real probe they ranked the culprit last.  The replacement waits for Bench 0.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .node import Node

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[-+]?\d+(?:\.\d+)?")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOP_WORDS = {
    "a", "an", "and", "arg", "args", "assert", "at", "bool", "by", "call",
    "class", "def", "dict", "error", "expected", "false", "file", "for",
    "from", "function", "got", "in", "is", "it", "line", "list", "module",
    "none", "not", "of", "on", "or", "out", "py", "python", "return", "self",
    "str", "test", "tests", "the", "this", "to", "true", "value", "with",
}
_DISPLAY_HELPERS = {
    "__eq__", "__format__", "__hash__", "__len__", "__repr__", "__str__",
    "asdict", "dict", "model_dump", "to_dict",
}
_INFRA_TOKENS = {
    "logger", "logging", "provider", "scope", "span", "telemetry", "trace",
    "tracer", "tracing",
}
_INFRA_ACTIONS = {"configure", "get", "init", "initialize", "set", "setup"}


def _stem(token: str) -> str:
    """Apply a small deterministic normalizer for code identifiers."""
    token = token.lower()
    for suffix in ("ization", "ational", "fulness", "iveness", "ments", "ment"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            token = token[: -len(suffix)]
            break
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            token = token[: -len(suffix)]
            break
    return token


def _tokens(*values: object) -> set[str]:
    output: set[str] = set()
    for value in values:
        split = _CAMEL.sub("_", str(value))
        for token in _TOKEN.findall(split):
            normalized = _stem(token)
            if len(normalized) > 1 and normalized not in _STOP_WORDS:
                output.add(normalized)
    return output


def _is_direct_test(node: Node) -> bool:
    parts = node.name.replace("<locals>", "").split(".")
    return any(part.startswith("test_") for part in parts if part)


def _is_test_source(node: Node) -> bool:
    source = (node.source_id or node.file).replace("\\", "/").lower()
    basename = source.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{source}"
        or basename.startswith("test_")
        or basename == "conftest.py"
    )


def _noise_penalty(node: Node) -> tuple[int, str | None]:
    leaf = node.name.rsplit(".", 1)[-1].lower()
    if leaf in _DISPLAY_HELPERS or leaf.startswith("__") and leaf.endswith("__"):
        return 34, "display/serialization helper"
    code_tokens = _tokens(node.name, node.module, node.source_id)
    if code_tokens & _INFRA_TOKENS and code_tokens & _INFRA_ACTIONS:
        return 28, "tracing/logging setup"
    if leaf in {"serialize", "deserialize", "dump", "dumps", "loads"}:
        return 20, "serialization helper"
    return 0, None


@dataclass(slots=True)
class _ObservedFrame:
    node: Node
    parent_id: str | None
    depth: int
    path: tuple[str, ...]
    completion: int


def _observed_frames(roots: list[Node]) -> list[_ObservedFrame]:
    frames: list[_ObservedFrame] = []
    completion = 0

    def visit(
        node: Node,
        parent_id: str | None,
        depth: int,
        path: tuple[str, ...],
    ) -> None:
        nonlocal completion
        here = path + (node.frame_id,)
        for child in node.children:
            visit(child, node.frame_id or parent_id, depth + 1, here)
        completion += 1
        frames.append(_ObservedFrame(node, parent_id, depth, here, completion))

    for root in roots:
        visit(root, None, 0, ())
    return frames


def rank_inspections(
    roots: list[Node],
    frame_index: dict,
    limit: int = 3,
    recorded_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Rank captured frames worth a focused rerun.

    Results are hypotheses for inspection, never bug locations.  Multiple calls
    to the same function are collapsed to their highest-scoring invocation.
    """
    observed = _observed_frames(roots)
    if not observed or limit <= 0:
        return []

    failures = [
        frame for frame in observed
        if frame.node.error is not None and not frame.node.handled
    ]
    if not failures:
        return []
    counts = Counter(frame.node.code_id for frame in observed)
    if recorded_counts:  # the run's own totals, when the observed frames are only a part of it
        counts = Counter(recorded_counts)
    short_ids = {
        frame.get("frame_id"): frame.get("id")
        for frame in frame_index.get("frames", [])
    }

    subtree_sizes: dict[int, int] = {}

    def collect_subtree(node: Node) -> int:
        size = 1 + sum(collect_subtree(child) for child in node.children)
        subtree_sizes[id(node)] = size
        return size

    for root in roots:
        collect_subtree(root)

    candidates: list[tuple[float, tuple, dict]] = []
    for frame in observed:
        node = frame.node
        if not node.frame_id or _is_direct_test(node):
            continue

        score = 10.0
        signals: list[str] = []
        distinct_callees = len({child.code_id for child in node.children})
        evidence: dict[str, object] = {
            "on_failed_branch": False,
            "completion_distance": None,
            "distinct_observed_callees": distinct_callees,
            "recorded_calls": counts[node.code_id],
            "mutated_inputs": [],
            "noise_penalty": None,
        }
        source_is_test = _is_test_source(node)
        if source_is_test:
            score -= 14
        else:
            score += 12

        closest_failure: _ObservedFrame | None = None
        closest_distance = math.inf
        for failure in failures:
            same_branch = (
                node.frame_id in failure.path or failure.node.frame_id in frame.path
            )
            if not same_branch:
                continue
            distance = abs(failure.completion - frame.completion)
            if distance < closest_distance:
                closest_distance = distance
                closest_failure = failure

        if closest_failure is not None:
            evidence["on_failed_branch"] = True
            evidence["completion_distance"] = closest_distance
            score += 26
            proximity = max(0.0, 16.0 - math.log2(closest_distance + 1) * 3.0)
            score += proximity
            if node.error is None:
                signals.append(
                    "recent completed project frame on the failed execution branch"
                )
            else:
                signals.append("exception recorded on the failed execution branch")

        mutated_inputs = sorted(node.input_changes)
        evidence["mutated_inputs"] = mutated_inputs[:6]
        if mutated_inputs:
            score += 32
            signals.insert(
                0,
                "caller-owned input changed in place during call: "
                + ", ".join(mutated_inputs[:3]),
            )

        if distinct_callees >= 2:
            score += min(16, math.log2(distinct_callees + 1) * 4)
            signals.append(f"coordinates {distinct_callees} distinct observed callees")

        # Prefer a narrow decision frame to a large wrapper that merely encloses it.
        score -= min(15, math.log2(subtree_sizes.get(id(node), 1)) * 2)

        if node.error is not None and not node.handled:
            score += 42
        elif node.error is not None:
            score += 4
        elif node.out not in (None, "None"):
            score += 6

        repetitions = counts[node.code_id]
        if repetitions > 1:
            score -= min(18, math.log2(repetitions) * 4)

        penalty, noise_reason = _noise_penalty(node)
        score -= penalty
        if noise_reason:
            evidence["noise_penalty"] = noise_reason
            signals.append(f"downranked as {noise_reason}")

        if not signals:
            signals.append("captured project frame near the end of the run")

        short_id = short_ids.get(node.frame_id)
        if short_id is None:
            continue
        payload = {
            "id": short_id,
            "frame_id": node.frame_id,
            "function": node.name,
            "source_id": node.source_id,
            "location": node.location,
            "score": round(score, 2),
            "reasons": signals[:3],
            "evidence": evidence,
            "selector": f"{node.code_id}#*" if repetitions > 1 else node.frame_id,
            "match_strategy": "all_calls" if repetitions > 1 else "exact_call",
        }
        tie_break = (
            source_is_test,
            -frame.completion,
            node.source_id,
            node.line,
            node.name,
            node.frame_id,
        )
        candidates.append((score, tie_break, payload))

    if any(not item[1][0] for item in candidates):
        candidates = [item for item in candidates if not item[1][0]]
    candidates.sort(key=lambda item: (-item[0], item[1]))
    best_score = candidates[0][0] if candidates else -math.inf
    unique: list[dict] = []
    seen_codes: set[str] = set()
    by_frame = {frame.node.frame_id: frame.node for frame in observed}
    for _, _, candidate in candidates:
        if candidate["score"] < best_score - 30:
            continue
        node = by_frame[candidate["frame_id"]]
        if node.code_id in seen_codes:
            continue
        seen_codes.add(node.code_id)
        unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique
