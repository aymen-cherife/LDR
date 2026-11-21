"""Phase 2 rules 16 and 17: how much slower the traced run is, and which functions are hot.

The untraced time comes from one untraced run that is asked for explicitly
(``--baseline``); LDR never runs the target twice on its own. A baseline is keyed by the
invocation together with a hash of every project file the traced run executed. Until the
code snapshots of Phase 3 exist, a changed file means ``baseline stale``.
"""

from __future__ import annotations

import json
import os
from hashlib import blake2b

BASELINE_FILE = "baselines.json"
NARROW_ABOVE = 5.0  # "about 5x": suggest --only above this
HOT_LISTED = 3
HOT_MIN_CALLS = 100


def file_hashes(paths) -> dict[str, str]:
    """A short content hash for each readable file, by the path it is shown under."""
    hashes = {}
    for display, path in paths:
        try:
            with open(path, "rb") as handle:
                hashes[display] = blake2b(handle.read(), digest_size=8).hexdigest()
        except OSError:
            continue
    return hashes


def read_baselines(ldr_dir: str) -> dict:
    try:
        with open(os.path.join(ldr_dir, BASELINE_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_baseline(ldr_dir: str, key: str, record: dict) -> None:
    from .store import atomic_json

    baselines = read_baselines(ldr_dir)
    baselines[key] = record
    os.makedirs(ldr_dir, exist_ok=True)
    atomic_json(os.path.join(ldr_dir, BASELINE_FILE), baselines)


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def slowdown(traced_seconds: float, baseline: dict | None, files: dict[str, str], cost: list[dict]) -> dict:
    """The factor, whether the baseline is stale, and the highest fingerprint costs."""
    if not baseline or not baseline.get("seconds"):
        return {"state": "no baseline"}
    factor = traced_seconds / baseline["seconds"]
    recorded = baseline.get("files") or {}
    stale = changed_files(recorded, files) if recorded else None
    return {
        "state": "stale" if stale else "measured",
        "factor": round(factor, 2),
        "traced_seconds": round(traced_seconds, 3),
        "untraced_seconds": round(baseline["seconds"], 3),
        "changed_files": (stale or [])[:10],
        "file_hashes_recorded": bool(recorded),
        "highest_cost": cost[:HOT_LISTED],
    }


def render_slowdown(info: dict) -> str:
    if info["state"] == "no baseline":
        return "SLOWDOWN: no baseline (run this command with --baseline once to record the untraced time).\n"
    text = (
        f"SLOWDOWN: {info['factor']:.1f}x (traced {info['traced_seconds']:.2f}s, "
        f"untraced {info['untraced_seconds']:.2f}s)"
    )
    if info["state"] == "stale":
        text += f"; baseline stale (changed since it was recorded: {', '.join(info['changed_files'])})"
    elif not info["file_hashes_recorded"]:
        text += "; baseline stale cannot be checked (it was recorded without file hashes)"
    if info["highest_cost"]:
        text += "; highest fingerprint cost: " + ", ".join(
            f"{item['function']} ({item['nodes']:,} nodes)" for item in info["highest_cost"]
        )
    if info["factor"] > NARROW_ABOVE:
        text += f". Above {NARROW_ABOVE:g}x: record less with --only <directory>"
    return text + ".\n"


def hot_functions(calls: dict[str, int], cost: dict[str, int], seconds: float) -> list[dict]:
    """The functions with the most calls: count, share of all calls, rate and fingerprint cost."""
    total = sum(calls.values())
    if total < HOT_MIN_CALLS:
        return []
    ranked = sorted(calls.items(), key=lambda item: (-item[1], item[0]))[:HOT_LISTED]
    return [
        {
            "function": function,
            "calls": count,
            "share": round(count / total, 4),
            "per_second": round(count / seconds) if seconds > 0 else None,
            "fingerprint_nodes": cost.get(function, 0),
        }
        for function, count in ranked
    ]


def render_hot(items: list[dict], total: int) -> str:
    parts = []
    for item in items:
        rate = f", ~{item['per_second']:,}/s" if item["per_second"] else ""
        parts.append(
            f"{item['function']} {item['calls']:,} calls ({item['share']:.1%}{rate}, "
            f"{item['fingerprint_nodes']:,} fingerprint nodes)"
        )
    return f"HOT FUNCTIONS (of {total:,} calls): " + "; ".join(parts) + ". They are recorded, not skipped.\n"
