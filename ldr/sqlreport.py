"""The default report, read from SQLite instead of from an in-memory tree.

A default run records one row per call in SQLite (``call_records``, or its staging table
until the run's indexes are built) and builds no tree. Everything the report says about
how many calls ran, which functions they were and where, is a query over those rows, so
no call is missing from it however many there were. The only nodes built are the failing
path (by the recorder, when an exception escapes) and a bounded sample of the first calls
for the readable trace; both are bounded by what a person can read.
"""

from __future__ import annotations

import json
import os
from contextlib import closing

from .node import Node

FRAME_LIMIT = 100_000  # frames listed in full; every frame that raised is listed beyond it
TRACE_LIMIT = 400  # calls turned into nodes for the readable trace of a run that passed


def _table(connection, run_id: str) -> str:
    """Records live in the staging table until the run's indexes are built."""
    row = connection.execute(
        "SELECT 1 FROM call_records_stage WHERE run_id = ? LIMIT 1", (run_id,)
    ).fetchone()
    return "call_records_stage" if row else "call_records"


def _open(store):
    if getattr(store, "remote", False):
        store.sync()  # wait until the parent has written everything that was sent
    return closing(store._connect())


def identity(function: str) -> str:
    """``source:qualname`` of a recorded function, without the test partition some workers add.

    A pytest worker prefixes each function with ``path::test/`` to keep per-test counters
    apart; the function is the same function in every test.
    """
    head, separator, rest = function.partition("::")
    if separator and "/" in rest:
        return rest.split("/", 1)[1]
    return function


def _qualname(function: str) -> str:
    function = identity(function)
    return function.split(":", 1)[1] if ":" in function else function


def _source(function: str) -> str:
    function = identity(function)
    return function.split(":", 1)[0] if ":" in function else ""


def is_folded(function: str) -> bool:
    """Generator expressions and lambdas are folded into their parent in the default view."""
    return _qualname(function).rsplit(".", 1)[-1] in ("<genexpr>", "<lambda>")


def is_test_function(function: str) -> bool:
    """The same rule as the tracer's test-node check, from a function's identity."""
    source = _source(function).replace("\\", "/").lower()
    basename = source.rsplit("/", 1)[-1]
    leaf = _qualname(function).rsplit(".", 1)[-1]
    return (
        leaf.startswith("test_") or "/tests/" in f"/{source}"
        or basename.startswith("test_") or basename == "conftest.py"
    )


def counts(store) -> dict:
    """Every recorded function's call and error counts: ``{function: (calls, errors)}``."""
    totals: dict[str, tuple[int, int]] = {}
    with _open(store) as connection:
        table = _table(connection, store.run_id)
        for function, calls, errors in connection.execute(
            f"SELECT function, COUNT(*), SUM(error IS NOT NULL) FROM {table} "
            "WHERE run_id = ? GROUP BY function", (store.run_id,),
        ):
            if is_folded(function):
                continue
            key = identity(function)
            before = totals.get(key, (0, 0))
            totals[key] = (before[0] + calls, before[1] + (errors or 0))
    return totals


def project_calls(per_function: dict) -> int:
    return sum(calls for function, (calls, _) in per_function.items() if not is_test_function(function))


def has_production_frame(per_function: dict, locations: dict, production_roots: list[str]) -> bool:
    for function in per_function:
        if is_test_function(function):
            continue
        filename = os.path.realpath(locations.get(function, ("", 0))[0]) if function in locations else ""
        for root in production_roots:
            try:
                if filename and os.path.commonpath((filename, root)) == root:
                    return True
            except ValueError:
                continue
    return False


def frame_index(store, per_function: dict, locations: dict) -> dict:
    """Frames in call order (the first FRAME_LIMIT and every one that raised) and per-function totals."""
    frames: list[dict] = []
    total = 0
    sample: dict[str, list[str]] = {}
    with _open(store) as connection:
        table = _table(connection, store.run_id)
        cursor = connection.execute(
            f"SELECT frame_id, parent, function, error FROM {table} WHERE run_id = ? "
            "ORDER BY worker, ordinal", (store.run_id,),
        )
        for address, parent, function, error in cursor:
            if is_folded(function):
                continue
            total += 1
            samples = sample.setdefault(identity(function), [])
            if address and len(samples) < 4:
                samples.append(address)
            elif address and samples and address != samples[-1]:
                samples[-1] = address
            if total <= FRAME_LIMIT or error is not None:
                file, line = locations.get(identity(function), ("", 0))
                frames.append({
                    "id": total,
                    "frame_id": address,
                    # An address is its parent's address plus one segment.
                    "parent_id": address.rsplit("/", 1)[0] if parent is not None else None,
                    "function": _qualname(function),
                    "location": f"{file}:{line}" if file else "",
                    "error": error,
                    "handled": False,
                })
    functions = []
    for function, (calls, errors) in per_function.items():
        file, line = locations.get(identity(function), ("", 0))
        source_id = _source(function)
        functions.append({
            "function": _qualname(function),
            "source_id": source_id,
            "location": f"{file}:{line}" if file else "",
            "recorded_calls": calls,
            "sample_frame_ids": sample.get(function, []),
            "inspect_selector": f"{function}#*",
            "error_count": errors,
        })
    functions.sort(key=lambda item: (item["source_id"], item["location"], item["function"]))
    return {"frames": frames, "functions": functions, "total": total, "truncated": total > len(frames)}


def trace_nodes(store, locations: dict, limit: int = TRACE_LIMIT) -> list[Node]:
    """The first calls of the run as nodes, for the readable trace of a run that passed."""
    nodes: dict[tuple[str, int], Node] = {}
    roots: list[Node] = []
    with _open(store) as connection:
        table = _table(connection, store.run_id)
        rows = connection.execute(
            f"SELECT worker, ordinal, frame_id, parent, function, error FROM {table} "
            "WHERE run_id = ? ORDER BY worker, ordinal LIMIT ?", (store.run_id, limit),
        ).fetchall()
        details = {
            address: json.loads(payload)
            for address, payload in connection.execute(
                "SELECT frame_id, payload FROM stream_calls WHERE run_id = ? "
                "AND frame_id IN (%s)" % ",".join("?" * len(rows)),
                (store.run_id, *[row[2] for row in rows]),
            )
        } if rows else {}
    for worker, ordinal, address, parent, function, error in rows:
        if is_folded(function):
            nodes[worker, ordinal] = nodes.get((worker, parent))  # its calls hang from its parent
            continue
        file, line = locations.get(identity(function), ("", 0))
        detail = details.get(address, {})
        node = Node(
            name=_qualname(function),
            in_=dict(detail.get("input_preview") or {}),
            out=detail.get("output_preview"),
            error=detail.get("error") or error,
            frame_id=address,
            file=file,
            source_id=_source(function),
            line=line,
        )
        nodes[worker, ordinal] = node
        owner = nodes.get((worker, parent)) if parent is not None else None
        # An exception its caller returned normally from was handled there.
        node.handled = error is not None and owner is not None and owner.error is None
        (owner.children if owner is not None else roots).append(node)
    return roots


FAILURE_LIMIT = 1500  # completed calls kept around a failure, the ones closest to it


def failure_tree(store, root_address: str, locations: dict, limit: int = FAILURE_LIMIT) -> Node | None:
    """The tree under one failing root: every call that raised and the last ``limit`` calls.

    The nearest completed calls are what ranking and the failing path read; further back
    they matter less, and a run can have millions. Parents that were not kept are skipped:
    a call hangs from its nearest kept ancestor.
    """
    prefix = root_address
    with _open(store) as connection:
        table = _table(connection, store.run_id)
        where = "run_id = ? AND (frame_id = ? OR substr(frame_id, 1, ?) = ?)"
        arguments = (store.run_id, prefix, len(prefix) + 1, prefix + "/")
        columns = "worker, ordinal, frame_id, parent, function, error"
        recent = connection.execute(
            f"SELECT {columns} FROM {table} WHERE {where} ORDER BY ordinal DESC LIMIT ?", (*arguments, limit)
        ).fetchall()
        raised = connection.execute(
            f"SELECT {columns} FROM {table} WHERE {where} AND error IS NOT NULL", arguments
        ).fetchall()
        top = connection.execute(f"SELECT {columns} FROM {table} WHERE run_id = ? AND frame_id = ?",
                                 (store.run_id, prefix)).fetchall()
        rows = sorted({(row[0], row[1]): row for row in [*recent, *raised, *top]}.values(),
                      key=lambda row: (row[0], row[1]))
        details = {}
        addresses = [row[2] for row in rows]
        for start in range(0, len(addresses), 500):
            chunk = addresses[start:start + 500]
            for address, payload in connection.execute(
                "SELECT frame_id, payload FROM stream_calls WHERE run_id = ? AND frame_id IN (%s)"
                % ",".join("?" * len(chunk)), (store.run_id, *chunk),
            ):
                details[address] = json.loads(payload)
    nodes: dict[str, Node] = {}
    root = None
    for worker, ordinal, address, parent, function, error in rows:
        if is_folded(function):
            continue
        file, line = locations.get(identity(function), ("", 0))
        detail = details.get(address, {})
        node = Node(
            name=_qualname(function), in_=dict(detail.get("input_preview") or {}),
            out=detail.get("output_preview"),
            # the stored detail has the exception's text; the record only its type
            error=detail.get("error") or error, frame_id=address, file=file,
            source_id=_source(function), line=line,
            ms=float(detail.get("elapsed_ms") or 0.0), timing=detail.get("timing"),
            input_changes=dict(detail.get("input_changes") or {}),
        )
        nodes[address] = node
        owner = None
        climb = address
        while "/" in climb and owner is None and climb != prefix:
            climb = climb.rsplit("/", 1)[0]
            owner = nodes.get(climb)
        if owner is not None:
            owner.children.append(node)
            node.handled = error is not None and owner.error is None
        elif root is None or address == prefix:
            root = node if address == prefix or root is None else root
    return nodes.get(prefix) or root
