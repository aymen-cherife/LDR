"""The previous run of the same target, and the comparison with it, through SQLite.

Every run is compared with the previous run of the same target (n against n-1):

* a pytest run is compared with the latest earlier pytest run that shares test node ids with
  it. Tests in both runs are compared; the others are listed as "not compared";
* any other run is compared with the latest earlier run of exactly the same command (runner,
  target, arguments and ``--only``; the scope LDR infers is not part of it).

Nothing here holds the previous run in memory. While the program runs, each finished call asks
SQLite for the signature of the call at the same address (``Lookup``); after the run, the whole
comparison is a join between the two runs' rows (``compare``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .store import invocation_key

KEEP_RUNS = 2  # runs kept per target; [tool.ldr.storage] keep_runs changes it

_ROOT = re.compile(r"(?:[^/:]+/)*[^/]*?::[^/]*")


def encode_node_id(node_id: str) -> str:
    """Keep the file path, and percent-encode ``%``, ``/`` and ``#`` after it.

    Those characters separate the segments of an address, so they must not
    appear inside the test's own segment.
    """
    path, separator, rest = node_id.partition("::")
    rest = rest.replace("%", "%25").replace("/", "%2F").replace("#", "%23")
    return path + separator + rest


def root_of(frame_id: str) -> str:
    """The test at the front of a call's address (``path::name``), or the address itself."""
    match = _ROOT.match(frame_id)
    return match.group(0) if match else frame_id


def _skipped(status: dict) -> bool:
    """A run that cannot be compared with: a rerun for detail, or one that did not finish."""
    return bool(status.get("rerun") or status.get("died") or status.get("incomplete"))


def _candidates(connection, run_id: str):
    row = connection.execute("SELECT invocation, started_ns FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None, []
    invocation = json.loads(row[0])
    earlier = connection.execute(
        "SELECT id, invocation, status FROM runs WHERE status IS NOT NULL AND id != ? "
        "AND (started_ns < ? OR (started_ns = ? AND id < ?)) ORDER BY started_ns DESC, id DESC",
        (run_id, row[1], row[1], run_id),
    ).fetchall()
    return invocation, earlier


def previous_run(connection, run_id: str) -> str | None:
    """The previous run of this run's target, or None. The run's own tests must be stored."""
    invocation, earlier = _candidates(connection, run_id)
    if invocation is None:
        return None
    key = invocation_key(invocation)
    for other, text, status in earlier:
        if _skipped(json.loads(status)):
            continue
        other_invocation = json.loads(text)
        if invocation.get("runner") == "pytest":
            if other_invocation.get("runner") != "pytest":
                continue
            shared = connection.execute(
                "SELECT 1 FROM run_tests a JOIN run_tests b ON a.node_id = b.node_id "
                "WHERE a.run_id = ? AND b.run_id = ? LIMIT 1", (run_id, other),
            ).fetchone()
            if shared:
                return other
        elif invocation_key(other_invocation) == key:
            return other
    return None


def provisional_run(connection, run_id: str) -> tuple[str | None, list[str] | None]:
    """Which run to look calls up in while the program runs, and which tests it covers.

    A pytest run's own tests are not known until they are collected, so the guess is the latest
    earlier pytest run of the same command, else the latest earlier pytest run; the final
    comparison (``previous_run``) is made afterwards from the tests both runs share.
    """
    invocation, earlier = _candidates(connection, run_id)
    if invocation is None:
        return None, None
    key = invocation_key(invocation)
    if invocation.get("runner") != "pytest":
        for other, text, status in earlier:
            if not _skipped(json.loads(status)) and invocation_key(json.loads(text)) == key:
                return other, None
        return None, None
    fallback = None
    for other, text, status in earlier:
        if _skipped(json.loads(status)) or json.loads(text).get("runner") != "pytest":
            continue
        if invocation_key(json.loads(text)) == key:
            fallback = other
            break
        fallback = fallback or other
    if fallback is None:
        return None, None
    roots = [
        encode_node_id(node) for (node,) in connection.execute(
            "SELECT DISTINCT node_id FROM run_tests WHERE run_id = ?", (fallback,)
        )
    ]
    return fallback, roots


class Lookup:
    """Per-call questions to the previous run, answered by SQLite (never held in memory)."""

    def __init__(self, path: str, run_id: str, roots=None, timeout: float = 30):
        self.run_id = run_id
        self.roots = frozenset(roots) if roots is not None else None
        self.connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=timeout)
        self.connection.execute("PRAGMA query_only = ON")

    def covers(self, frame_id: str) -> bool:
        """Whether this call is in the part of the previous run that is compared."""
        return self.roots is None or root_of(frame_id) in self.roots

    def signature(self, frame_id: str):
        row = self.connection.execute(
            "SELECT signature FROM call_records WHERE run_id = ? AND frame_id = ? LIMIT 1",
            (self.run_id, frame_id),
        ).fetchone()
        return None if row is None else bytes(row[0])

    def close(self) -> None:
        self.connection.close()


def _records(connection, run_id: str) -> str:
    row = connection.execute("SELECT 1 FROM call_records_stage WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
    return "call_records_stage" if row else "call_records"


def tests_of(connection, run_id: str) -> set[str]:
    return {node for (node,) in connection.execute("SELECT DISTINCT node_id FROM run_tests WHERE run_id = ?", (run_id,))}


def same_files(before: dict, after: dict) -> bool:
    """Every file executed by both runs has the same content hash in both."""
    shared = before.keys() & after.keys()
    return bool(shared) and all(before[name] == after[name] for name in shared)


def compare(
    connection,
    run_id: str,
    previous: str,
    *,
    prefix: str | None = None,
    files: tuple[dict, dict] | None = None,
) -> dict:
    """The run against ``previous``, by a join over their rows. The status block the report reads.

    For pytest the comparison covers the tests present in both runs (or only ``prefix``, a
    rerun's one test); the others are counted as not compared. ``files`` are the two runs' file
    hashes: when every file both executed is unchanged, a difference carries the note.
    """
    pytest = connection.execute("SELECT 1 FROM run_tests WHERE run_id = ? LIMIT 1", (run_id,)).fetchone() is not None
    ours, theirs = _records(connection, run_id), _records(connection, previous)
    result: dict = {"run_id": previous, "error": None}
    covered = None
    if prefix is not None:
        covered = {prefix}
    elif pytest:
        now, before = tests_of(connection, run_id), tests_of(connection, previous)
        covered = {encode_node_id(node) for node in now & before}
        result["tests"] = {
            "compared": len(now & before),
            "only_in_this_run": sorted(now - before),
            "only_in_previous": sorted(before - now),
        }
    connection.create_function("ldr_root", 1, root_of, deterministic=True)
    connection.create_function(
        "ldr_covered", 1, (lambda frame_id: 1) if covered is None else (lambda frame_id: int(root_of(frame_id) in covered)),
        deterministic=True,
    )
    connection.execute("DROP TABLE IF EXISTS temp.now")
    connection.execute(
        "CREATE TEMP TABLE now AS SELECT frame_id, function, signature, reference FROM "
        f"{ours} WHERE run_id = ? AND ldr_covered(frame_id)", (run_id,),
    )
    connection.execute("CREATE INDEX temp.now_frame ON now(frame_id)")
    counts = {"compared": 0, "same": 0, "different": 0, "new": 0, "not_compared": 0}
    changed: dict[str, int] = {}
    for function, state, number in connection.execute(
        "SELECT n.function, CASE WHEN p.frame_id IS NULL THEN 'new' "
        "WHEN p.signature != n.signature THEN 'different' "
        "WHEN n.reference IS NOT NULL THEN 'not_compared' ELSE 'same' END AS state, COUNT(*) "
        f"FROM temp.now n LEFT JOIN {theirs} p ON p.run_id = ? AND p.frame_id = n.frame_id "
        "GROUP BY n.function, state", (previous,),
    ):
        counts[state] += number
        if state != "new":
            counts["compared"] += number
        if state in ("different", "new"):
            changed[function] = changed.get(function, 0) + number
    missing = 0
    for function, number in connection.execute(
        f"SELECT p.function, COUNT(*) FROM {theirs} p LEFT JOIN temp.now n ON n.frame_id = p.frame_id "
        "WHERE p.run_id = ? AND ldr_covered(p.frame_id) AND n.frame_id IS NULL GROUP BY p.function",
        (previous,),
    ):
        missing += number
        changed[function] = changed.get(function, 0) + number
    connection.execute("DROP TABLE temp.now")
    varied = bool(files) and same_files(*files)
    result.update(
        counts, missing=missing, changed=sorted(changed),
        changed_detail={function: [number, number if varied else 0] for function, number in sorted(changed.items())},
        files_unchanged=varied if files else None,
    )
    return result


def keep_runs(project_root: str) -> int:
    """``[tool.ldr.storage] keep_runs``: how many runs of each target are kept."""
    import tomllib

    try:
        with open(Path(project_root) / "pyproject.toml", "rb") as handle:
            value = tomllib.load(handle).get("tool", {}).get("ldr", {}).get("storage", {}).get("keep_runs", KEEP_RUNS)
    except (OSError, tomllib.TOMLDecodeError):
        return KEEP_RUNS
    return value if type(value) is int and value >= 1 else KEEP_RUNS


_TABLES = (
    "workers", "calls", "stream_budget", "stream_calls", "call_records", "call_lookup",
    "call_records_stage", "call_lookup_stage", "stream_summary", "stream_value_budget",
    "stream_values", "stream_deep_budget", "stream_deep_frames", "stream_deep_steps",
    "stream_boundary_budget", "stream_boundaries", "run_environment", "run_tests", "run_rng",
    "run_sections", "value_sizes",
)


def prune(directory: str, keep: int = KEEP_RUNS) -> list[str]:
    """Delete every run of a target beyond the newest ``keep``. Returns the ids deleted.

    A target is a command (runner, target, arguments, ``--only``). A rerun made for detail
    belongs to the run it repeats: it is neither counted nor kept without it.
    """
    path = Path(directory, "traces.sqlite3")
    if not path.exists():
        return []
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        groups: dict[str, list[str]] = {}
        reruns: dict[str, list[str]] = {}
        for run_id, text, status in connection.execute(
            "SELECT id, invocation, status FROM runs WHERE status IS NOT NULL ORDER BY started_ns DESC, id DESC"
        ):
            status = json.loads(status)
            if status.get("rerun"):
                reruns.setdefault(status["rerun"].get("of"), []).append(run_id)
            else:
                groups.setdefault(invocation_key(json.loads(text)), []).append(run_id)
        doomed = [run for runs in groups.values() for run in runs[keep:]]
        doomed += [rerun for run in doomed for rerun in reruns.get(run, [])]
        if not doomed:
            return []
        with connection:
            for chunk in (doomed[i:i + 200] for i in range(0, len(doomed), 200)):
                marks = ",".join("?" * len(chunk))
                for table in _TABLES:
                    connection.execute(f"DELETE FROM {table} WHERE run_id IN ({marks})", chunk)
                connection.execute(f"DELETE FROM runs WHERE id IN ({marks})", chunk)
        return doomed
