"""The previous run of the same target, and the comparison recorded during execution.

Every run is compared with the previous run of the same target (n against n-1):

* a pytest run is compared with the latest earlier pytest run that shares test node ids with
  it. Tests in both runs are compared; the others are listed as "not compared";
* any other run is compared with the latest earlier run of exactly the same command (runner,
  target, arguments and ``--only``; the scope LDR infers is not part of it).

The recorder loads the previous run's signatures once and looks them up in memory.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from .store import invocation_key

KEEP_RUNS = 2  # runs kept per target; [tool.ldr.storage] keep_runs changes it
VARIED_RUNS = 2  # the two consecutive runs of identical files that the note speaks of
VARIED_NOTE = "also varied across {runs} runs with no edit"

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


def script_run(connection, run_id: str) -> str | None:
    """For a script (not pytest): the latest earlier run of exactly the same command. A pytest run
    chooses its previous run once it has collected its tests (``run_for_tests``)."""
    invocation, earlier = _candidates(connection, run_id)
    if invocation is None or invocation.get("runner") == "pytest":
        return None
    key = invocation_key(invocation)
    for other, text, status in earlier:
        if not _skipped(json.loads(status)) and invocation_key(json.loads(text)) == key:
            return other
    return None


class Previous:
    """The signatures of the previous run's calls, by address, held in memory.

    Loaded in one query, so that a call asks a dict and never SQLite. ``roots`` limits it to the
    tests (or the one test of a rerun) that both runs have; a call outside them is not compared.
    """

    def __init__(self, run_id: str, table: dict, roots=None, functions=()):
        self.run_id = run_id
        self.table = table
        self.functions = functions
        self.roots = frozenset(roots) if roots is not None else None

    def covers(self, frame_id: str) -> bool:
        """Whether this call is in the part of the previous run that is compared."""
        return self.roots is None or root_of(frame_id) in self.roots

    def signature(self, frame_id: str):
        entry = self.table.get(frame_id)
        return entry[0] if entry is not None else None

    def addresses(self):
        return self.table.keys()

    def __len__(self) -> int:
        return len(self.table)


def load(connection, run_id: str, roots=None) -> Previous:
    """Read the previous run's (address, signature) rows with one query."""
    wanted = frozenset(roots) if roots is not None else None
    names = {}
    table = {}
    rows = connection.execute(
        "SELECT frame_id, signature, function FROM call_records WHERE run_id = ?", (run_id,)
    )
    for address, signature, function in rows:
        if wanted is None or root_of(address) in wanted:
            table[address] = (bytes(signature), names.setdefault(function, len(names)))
    return Previous(run_id, table, wanted, tuple(names))


def run_for_tests(connection, run_id: str, node_ids, *, replicated=False) -> tuple[str | None, list[str] | None]:
    """Once a pytest run has collected its tests: the latest earlier pytest run that shares a test
    with it, and the (encoded) ids of the tests they share."""
    invocation, earlier = _candidates(connection, run_id)
    if invocation is None:
        return None, None
    mine = set(node_ids)
    for other, text, status in earlier:
        if _skipped(json.loads(status)) or json.loads(text).get("runner") != "pytest":
            continue
        theirs = tests_of(connection, other)
        if replicated:
            shared = {
                node for node in theirs
                if re.match(r"gw\d+/", node) and node.split("/", 1)[1] in mine
            }
        else:
            shared = theirs & mine
        if shared:
            return other, [encode_node_id(node) for node in sorted(shared)]
    return None, None


def tests_of(connection, run_id: str) -> set[str]:
    return {node for (node,) in connection.execute("SELECT DISTINCT node_id FROM run_tests WHERE run_id = ?", (run_id,))}


def same_files(before: dict, after: dict) -> bool:
    """Both runs executed the same files with the same content hashes."""
    return bool(before) and before == after


class Comparison:
    """Collect committed verdict counts and merge addresses across recording workers."""

    def __init__(self, previous, files, tests):
        self.previous = previous
        self.files = files
        self.tests = tests
        self.seen = set()
        self.counts = {}
        self.last_address = None

    def record(self, fingerprints):
        for _, address, _, function, _, _, _, _, verdict in fingerprints:
            self.last_address = address
            if verdict is None:
                continue
            if verdict != "new":
                self.seen.add(address)
            root = root_of(address) if self.tests is not None else None
            by_function = self.counts.setdefault(root, {})
            key = (function, verdict)
            by_function[key] = by_function.get(key, 0) + 1

    def result(self, files, tests, stopped=None):
        counts = {"compared": 0, "same": 0, "different": 0, "new": 0, "not_compared": 0}
        changed = {}
        result = {"run_id": self.previous.run_id, "error": None}
        if stopped and self.tests is not None:
            # A process can die before pytest flushes the list of tests it started.
            tests = tests | {node for node in self.tests if encode_node_id(node) in self.counts}
        shared = tests & self.tests if self.tests is not None else None
        roots = {encode_node_id(node) for node in shared} if shared is not None else None
        if shared is not None:
            result["tests"] = {
                "compared": len(shared),
                "only_in_this_run": sorted(tests - self.tests),
                "only_in_previous": sorted(self.tests - tests),
            }
        for root, grouped in self.counts.items():
            if roots is not None and root not in roots:
                continue
            for (function, verdict), number in grouped.items():
                key = {"changed": "different", "not compared": "not_compared"}.get(verdict, verdict)
                counts[key] += number
                if verdict != "new":
                    counts["compared"] += number
                if verdict in ("changed", "new"):
                    changed[function] = changed.get(function, 0) + number
        missing = {}
        if stopped:
            result["stopped"] = {"reason": stopped, "address": self.last_address}
        else:
            for address in self.previous.addresses() - self.seen:
                if roots is None or root_of(address) in roots:
                    function = self.previous.functions[self.previous.table[address][1]]
                    missing.setdefault(function, []).append(address)
                    changed[function] = changed.get(function, 0) + 1
        varied = files is not None and same_files(self.files, files)
        result.update(
            counts, missing=sum(map(len, missing.values())), changed=sorted(changed),
            changed_detail={function: [number, number if varied else 0] for function, number in sorted(changed.items())},
            files_unchanged=varied if files is not None else None,
        )
        return result, missing


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


def doomed(connection, keep: int) -> list[str]:
    """The runs beyond the newest ``keep`` of each target, with the reruns that belong to them.

    A target is a command (runner, target, arguments, ``--only``). A rerun made for detail
    belongs to the run it repeats: it is neither counted nor kept without it.
    """
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
    found = [run for runs in groups.values() for run in runs[keep:]]
    found += [rerun for run in found for rerun in reruns.get(run, [])]
    return found


def prune(directory: str, keep: int = KEEP_RUNS) -> list[str]:
    """Delete every run of a target beyond the newest ``keep``. Returns the ids deleted."""
    path = Path(directory, "traces.sqlite3")
    if not path.exists():
        return []
    from .supervisor import read_state

    directories = {}
    live = set()
    for directory in path.parent.glob("runs/*"):
        state = read_state(directory)
        if state and (run_id := state.get("store_run_id")):
            if state.get("state") == "finished":
                directories[run_id] = directory
            else:
                live.add(run_id)
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        found = [run_id for run_id in doomed(connection, keep) if run_id not in live]
        if not found:
            return []
        with connection:
            for chunk in (found[i:i + 200] for i in range(0, len(found), 200)):
                marks = ",".join("?" * len(chunk))
                for table in _TABLES:
                    connection.execute(f"DELETE FROM {table} WHERE run_id IN ({marks})", chunk)
                connection.execute(f"DELETE FROM runs WHERE id IN ({marks})", chunk)
        shrink(connection)
    for run_id in found:
        if directory := directories.get(run_id):
            shutil.rmtree(directory, ignore_errors=True)
    return found


def shrink(connection: sqlite3.Connection) -> bool:
    """Give the pages freed by deleted runs back to the file system. True when it was done.

    A database made with incremental auto-vacuum frees just the empty pages at the end of
    the file. An older database is converted by one VACUUM, which rewrites it once. If
    another process holds the database, the file stays as it is: nothing is lost.
    """
    try:
        if connection.execute("PRAGMA auto_vacuum").fetchone() == (2,):
            connection.execute("PRAGMA incremental_vacuum").fetchall()
        else:
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return True
    except sqlite3.Error:
        return False
