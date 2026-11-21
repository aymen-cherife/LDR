"""The short result written to ``.ldr/last.json``.

``last.json`` is a summary: it stays under ``LIMIT`` characters however large the run.
Whatever does not fit is kept whole in the run's SQLite database (table
``run_sections``) and ``last.json`` says which parts were cut and how many entries each
had. Nothing is dropped silently.
"""

from __future__ import annotations

import json
from typing import Any

LIMIT = 30_000
SHOWN = 25
# Parts of the result that can grow with the run, and are kept whole when cut.
SECTIONS = ("frame_index", "inspection", "findings", "failures", "suggestions", "scope", "behavior")


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _at(payload: dict, path: tuple[str, ...]):
    for key in path:
        payload = payload[key]
    return payload


def _lists(payload: dict) -> list[tuple[tuple[str, ...], list]]:
    """Every list in a section, with its path: the section itself or one level down."""
    found = []
    for key in SECTIONS:
        value = payload.get(key)
        if isinstance(value, list):
            found.append(((key,), value))
        elif isinstance(value, dict):
            found.extend(((key, inner), item) for inner, item in value.items() if isinstance(item, list))
    return found


def _frames_to_show(frames: list) -> list:
    """Frames that raised first, then the earliest ones, in their original order."""
    chosen = {id(frame) for frame in [f for f in frames if f.get("error")][:SHOWN]}
    for frame in frames:
        if len(chosen) >= SHOWN:
            break
        chosen.add(id(frame))
    return [frame for frame in frames if id(frame) in chosen]


def _functions_to_show(functions: list) -> list:
    ranked = sorted(
        range(len(functions)),
        key=lambda i: (-functions[i].get("error_count", 0), -functions[i].get("recorded_calls", 0), i),
    )[:SHOWN]
    return [functions[i] for i in sorted(ranked)]


def summarize(payload: dict) -> tuple[dict, dict]:
    """Return (the short payload, the whole sections that were cut, by name)."""
    short = dict(payload)
    for key in SECTIONS:  # one-level copies, so the caller's payload is never changed
        if isinstance(short.get(key), dict):
            short[key] = dict(short[key])
    totals: dict[tuple[str, ...], int] = {}

    def keep(path: tuple[str, ...], items: list) -> None:
        totals.setdefault(path, len(_at(payload, path)))
        _at(short, path[:-1])[path[-1]] = items

    index = short.get("frame_index")
    if isinstance(index, dict):
        if len(index.get("frames", ())) > SHOWN:
            keep(("frame_index", "frames"), _frames_to_show(index["frames"]))
        if len(index.get("functions", ())) > SHOWN:
            keep(("frame_index", "functions"), _functions_to_show(index["functions"]))
    for path, items in _lists(short):
        if len(items) > SHOWN:
            keep(path, items[:SHOWN])
    for _ in range(400):
        if _size(short) <= LIMIT:
            break
        candidates = [(path, items) for path, items in _lists(short) if items]
        if not candidates:
            break
        path, items = max(candidates, key=lambda pair: _size(pair[1]))
        keep(path, items[: len(items) // 2])
    if not totals:
        return short, {}
    names = sorted({path[0] for path in totals})
    short["summary"] = {
        "limit_chars": LIMIT,
        "cut": [
            {"part": ".".join(path), "shown": len(_at(short, path)), "total": total}
            for path, total in sorted(totals.items())
        ],
        "whole_in": {
            "database": ".ldr/traces.sqlite3",
            "table": "run_sections",
            "run_id": (payload.get("status") or {}).get("run_id"),
            "names": names,
        },
    }
    return short, {name: payload[name] for name in names}
