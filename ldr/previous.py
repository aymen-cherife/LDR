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

    def __init__(self, run_id: str, table: dict, roots=None):
        self.run_id = run_id
        self.table = table
        self.roots = frozenset(roots) if roots is not None else None

    def covers(self, frame_id: str) -> bool:
        """Whether this call is in the part of the previous run that is compared."""
        return self.roots is None or root_of(frame_id) in self.roots

    def signature(self, frame_id: str):
        return self.table.get(frame_id)

    def addresses(self):
        return self.table.keys()

    def __len__(self) -> int:
        return len(self.table)


def load(connection, run_id: str, roots=None) -> Previous:
    """Read the previous run's (address, signature) rows with one query."""
    wanted = frozenset(roots) if roots is not None else None
    rows = connection.execute(
        "SELECT frame_id, signature FROM call_records WHERE run_id = ?", (run_id,)
    )
    if wanted is None:
        table = {address: bytes(signature) for address, signature in rows}
    else:
        table = {address: bytes(signature) for address, signature in rows if root_of(address) in wanted}
    return Previous(run_id, table, wanted)


def run_for_tests(connection, run_id: str, node_ids) -> tuple[str | None, list[str] | None]:
    """Once a pytest run has collected its tests: the latest earlier pytest run that shares a test
    with it, and the (encoded) ids of the tests they share."""
    invocation, earlier = _candidates(connection, run_id)
    if invocation is None:
        return None, None
    mine = set(node_ids)
    for other, text, status in earlier:
        if _skipped(json.loads(status)) or json.loads(text).get("runner") != "pytest":
            continue
        shared = tests_of(connection, other) & mine
        if shared:
            return other, [encode_node_id(node) for node in sorted(shared)]
    return None, None


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
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        found = doomed(connection, keep)
        if not found:
            return []
        with connection:
            for chunk in (found[i:i + 200] for i in range(0, len(found), 200)):
                marks = ",".join("?" * len(chunk))
                for table in _TABLES:
                    connection.execute(f"DELETE FROM {table} WHERE run_id IN ({marks})", chunk)
                connection.execute(f"DELETE FROM runs WHERE id IN ({marks})", chunk)
        shrink(connection)
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
