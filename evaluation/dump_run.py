"""Fingerprint what the latest run in DIR stored and answered, for before/after checks.

    python evaluation/dump_run.py DIR            # print one JSON document
    python evaluation/dump_run.py DIR OTHER_JSON # exit 1 and list the differences

Every stored table is hashed with run ids and timings removed. The same code run twice
(the A/A check) must give no differences; a change that must not alter what is stored
gives none either. Only timings, run identifiers and run times are dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys

TIMING = {
    "elapsed_ms", "own_ms", "children_ms", "suspended_ms", "timing", "ms", "execution_seconds",
    "run_id", "started_ns", "postprocess_seconds", "total_seconds", "seconds", "cost_ms",
    "total_ms", "baseline_run", "reference_run",
}
TABLES = {
    # table: (columns hashed, order by)
    "call_records": ("worker, ordinal, frame_id, parent, function, error, reference, signature, parts", "worker, ordinal"),
    "call_lookup": ("fp, worker, ordinal, role, name", "fp, worker, ordinal, role, name"),
    "stream_calls": ("worker, ordinal, frame_id, parent, payload", "worker, ordinal"),
    "stream_values": ("worker, ordinal, phase, name, payload", "worker, ordinal, phase, name"),
    "stream_deep_frames": ("worker, ordinal, observed, retained", "worker, ordinal"),
    "stream_deep_steps": ("worker, ordinal, step, payload", "worker, ordinal, step"),
    "stream_boundaries": ("worker, ordinal, payload", "worker, ordinal"),
    "stream_summary": ("worker, summary", "worker"),
    "calls": ("worker, ordinal, parent, payload, values_fingerprint", "worker, ordinal"),
}
_CLOCK = re.compile(rb"\d+(\.\d+)?\s?(ms|s)\b")


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k not in TIMING}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def normal(cell):
    if isinstance(cell, bytes):
        try:
            return json.dumps(clean(json.loads(cell)), sort_keys=True)
        except ValueError:
            return cell.hex()
    if isinstance(cell, str) and cell[:1] in "{[":
        try:
            return json.dumps(clean(json.loads(cell)), sort_keys=True)
        except ValueError:
            return cell
    return repr(cell)


def dump(directory):
    database = os.path.join(directory, ".ldr", "traces.sqlite3")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    run = connection.execute("SELECT id FROM runs ORDER BY started_ns DESC LIMIT 1").fetchone()[0]
    result = {}
    for table, (columns, order) in TABLES.items():
        digest = hashlib.sha256()
        count = 0
        query = f"SELECT {columns} FROM {table} WHERE run_id = ? ORDER BY {order}"
        for row in connection.execute(query, (run,)):
            digest.update("\x1f".join(normal(cell) for cell in row).encode())
            digest.update(b"\x1e")
            count += 1
        result[table] = {"rows": count, "sha": digest.hexdigest()[:16]}
    path = os.path.join(directory, ".ldr", "last.json")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            document = clean(json.loads(handle.read()))
        text = json.dumps(document, sort_keys=True)
        result["last.json"] = {"chars": len(text), "sha": hashlib.sha256(text.encode()).hexdigest()[:16]}
    return result


def main():
    result = dump(sys.argv[1])
    if len(sys.argv) == 2:
        print(json.dumps(result, indent=1, sort_keys=True))
        return 0
    with open(sys.argv[2], encoding="utf-8") as handle:
        other = json.load(handle)
    different = [name for name in sorted(set(result) | set(other)) if result.get(name) != other.get(name)]
    print(f"differences: {len(different)}" + (f" {different}" if different else ""))
    return 1 if different else 0


if __name__ == "__main__":
    sys.exit(main())
