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
from contextlib import closing

TIMING = {
    "elapsed_ms", "own_ms", "children_ms", "suspended_ms", "timing", "ms", "execution_seconds",
    "run_id", "started_ns", "postprocess_seconds", "total_seconds", "seconds", "cost_ms",
    "total_ms", "baseline_run", "reference_run",
    "per_second", "factor", "traced_seconds", "untraced_seconds",  # rates and ratios
}
TABLES = {
    # table: (columns hashed, order by)
    "call_records": ("worker, ordinal, frame_id, parent, function, error, reference, signature, parts", "worker, ordinal"),
    "call_lookup": ("fp, worker, ordinal, role, name", "fp, worker, ordinal, role, name"),
    "value_sizes": ("worker, address, phase, role, name, type, nodes", "worker, address, phase, role, name, type, nodes"),
    "stream_calls": ("worker, ordinal, frame_id, parent, payload", "worker, ordinal"),
    "stream_values": ("worker, ordinal, phase, name, payload", "worker, ordinal, phase, name"),
    "stream_deep_frames": ("worker, ordinal, observed, retained", "worker, ordinal"),
    "stream_deep_steps": ("worker, ordinal, step, payload", "worker, ordinal, step"),
    "stream_boundaries": ("worker, ordinal, payload", "worker, ordinal"),
    "stream_summary": ("worker, summary", "worker"),
    "calls": ("worker, ordinal, parent, payload, values_fingerprint", "worker, ordinal"),
}
TIMING |= {key for key in os.environ.get("DUMP_DROP", "").split(",") if key}  # changed on purpose


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


def verdicts(connection, run):
    """Use recorded verdicts when available. Derive the old SQL verdicts for the baseline."""
    from ldr.previous import encode_node_id, root_of

    status, = connection.execute("SELECT status FROM runs WHERE id = ?", (run,)).fetchone()
    previous = json.loads(status).get("reference", {}).get("run_id")
    if not previous:
        return []
    query = """SELECT frame_id, function, signature, reference FROM call_records
               WHERE run_id = ? ORDER BY worker, ordinal"""
    old = {address: (function, signature) for address, function, signature, _
           in connection.execute(query, (previous,))}
    roots = [
        {encode_node_id(node) for node, in connection.execute(
            "SELECT DISTINCT node_id FROM run_tests WHERE run_id = ?", (key,)
        )}
        for key in (run, previous)
    ]
    shared = roots[0] & roots[1] if roots[0] else None
    columns = {column[1] for column in connection.execute("PRAGMA table_info(call_records)")}
    if "verdict" in columns:
        stored = dict(connection.execute("SELECT frame_id, verdict FROM call_records WHERE run_id = ?", (run,)))
    else:
        stored = None
    seen = set()
    rows = []
    for address, function, signature, quality in connection.execute(query, (run,)):
        seen.add(address)
        if stored is not None:
            verdict = stored[address] or "not compared"
        elif shared is not None and root_of(address) not in shared:
            verdict = "not compared"
        elif address not in old:
            verdict = "new"
        elif signature != old[address][1]:
            verdict = "changed"
        else:
            verdict = "not compared" if quality is not None else "same"
        rows.append([address, function, verdict])
    if stored is None:
        missing = [(address, function) for address, (function, _) in old.items()
                   if address not in seen and (shared is None or root_of(address) in shared)]
    else:
        saved = connection.execute(
            "SELECT payload FROM run_sections WHERE run_id = ? AND name = 'missing_calls'", (run,)
        ).fetchone()
        missing = [(address, function) for function, addresses in json.loads(saved[0]).items()
                   for address in addresses] if saved else []
    rows.extend([address, function, "missing"] for address, function in missing)
    return sorted(rows)


def dump(directory, *, include_records=False, include_verdicts=False):
    database = os.path.join(directory, ".ldr", "traces.sqlite3")
    result = {}
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        run = connection.execute("SELECT id FROM runs ORDER BY started_ns DESC LIMIT 1").fetchone()[0]
        for table, (columns, order) in TABLES.items():
            digest = hashlib.sha256()
            count = 0
            records = []
            query = f"SELECT {columns} FROM {table} WHERE run_id = ? ORDER BY {order}"
            for row in connection.execute(query, (run,)):
                normalized = [normal(cell) for cell in row]
                digest.update("\x1f".join(normalized).encode())
                digest.update(b"\x1e")
                count += 1
                if include_records:
                    records.append(normalized)
            result[table] = {"rows": count, "sha": digest.hexdigest()[:16]}
            if include_records:
                result[table]["records"] = records
        if include_verdicts:
            result["verdicts"] = verdicts(connection, run)
    path = os.path.join(directory, ".ldr", "last.json")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            document = clean(json.loads(handle.read()))
        if include_records:
            status = document.get("status", {})
            status.get("reference", {}).pop("run_id", None)
            status.pop("machine", None)  # machine telemetry comes from outside the recording
            status.pop("previous_load_s", None)
            previous = document.get("behavior", {})
            if isinstance(previous.get("reference"), str):
                previous.pop("reference")
            for section in TABLES:
                if section == "stream_summary":
                    # The previous-run ID and load duration differ across independent recordings.
                    records = result[section]["records"]
                    cleaned = []
                    for worker, raw in records:
                        summary = json.loads(raw)
                        summary.pop("previous_load_s", None)
                        cleaned.append([worker, json.dumps(summary, sort_keys=True)])
                    result[section] = cleaned
        text = json.dumps(document, sort_keys=True)
        result["last.json"] = {"chars": len(text), "sha": hashlib.sha256(text.encode()).hexdigest()[:16]}
        if include_records:
            result["last.json"]["records"] = document
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
