"""SQLite persistence for recorded calls and trace trees."""

import json
import marshal
import os
import shutil
import sqlite3
import tomllib
from contextlib import closing
from hashlib import sha256
from itertools import islice
from pathlib import Path
from time import monotonic, sleep, time_ns
from uuid import uuid4

from . import previous
from .fingerprint import DEFAULT_WALK_CEILING, is_digest
from .node import Node, capture_error

DEFAULT_BATCH_SIZE = 1000
DEFAULT_BUSY_TIMEOUT = 30
# Phase 2 rule 2. Placeholders, TO MEASURE: detail for the first N calls of each
# function, and (on demand, later) the last K.
DEFAULT_FIRST_CALLS = 8
DEFAULT_LAST_CALLS = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started_ns INTEGER NOT NULL, invocation TEXT NOT NULL, status TEXT
);
CREATE TABLE IF NOT EXISTS workers (
    run_id TEXT NOT NULL REFERENCES runs(id), name TEXT NOT NULL, summary TEXT,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS captured_values (
    fingerprint BLOB NOT NULL PRIMARY KEY, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    parent INTEGER, payload TEXT NOT NULL,
    values_fingerprint BLOB NOT NULL REFERENCES captured_values(fingerprint),
    PRIMARY KEY (run_id, worker, ordinal),
    FOREIGN KEY (run_id, worker) REFERENCES workers(run_id, name)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stream_budget (
    run_id TEXT PRIMARY KEY REFERENCES runs(id), used_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stream_calls (
    run_id TEXT NOT NULL REFERENCES runs(id), worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    frame_id TEXT NOT NULL, parent INTEGER, payload BLOB NOT NULL,
    PRIMARY KEY (run_id, worker, ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS call_records (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    frame_id TEXT NOT NULL, parent INTEGER, function TEXT NOT NULL, error TEXT,
    reference TEXT, signature BLOB NOT NULL, parts BLOB NOT NULL, verdict TEXT,
    PRIMARY KEY (run_id, worker, ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS call_lookup (
    run_id TEXT NOT NULL, fp TEXT NOT NULL, worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    role TEXT NOT NULL, name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_lookup_fp ON call_lookup(run_id, fp);
-- Readers look up individual calls by address.
CREATE INDEX IF NOT EXISTS call_records_frame ON call_records(run_id, frame_id);
-- Records arrive here without indexes and move to the tables above once per run.
-- The size of every value the fingerprint walk read: its type, its role in the call
-- (receiver, argument or return), the call's address and the nodes walked.
CREATE TABLE IF NOT EXISTS value_sizes (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, address TEXT NOT NULL,
    phase TEXT NOT NULL, role TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
    nodes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS call_records_stage (
    run_id TEXT, worker TEXT, ordinal INTEGER, frame_id TEXT, parent INTEGER,
    function TEXT, error TEXT, reference TEXT, signature BLOB, parts BLOB, verdict TEXT
);
CREATE TABLE IF NOT EXISTS call_lookup_stage (
    run_id TEXT, fp TEXT, worker TEXT, ordinal INTEGER, role TEXT, name TEXT
);
CREATE TABLE IF NOT EXISTS stream_summary (
    run_id TEXT NOT NULL REFERENCES runs(id), worker TEXT NOT NULL, summary TEXT NOT NULL,
    PRIMARY KEY (run_id, worker)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS stream_calls_frame ON stream_calls(run_id, frame_id);
CREATE TABLE IF NOT EXISTS stream_value_budget (
    run_id TEXT PRIMARY KEY REFERENCES runs(id), used_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stream_values (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    phase TEXT NOT NULL, name TEXT NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (run_id, worker, ordinal, phase, name),
    FOREIGN KEY (run_id, worker, ordinal) REFERENCES stream_calls(run_id, worker, ordinal)
);
CREATE TABLE IF NOT EXISTS stream_deep_budget (
    run_id TEXT PRIMARY KEY REFERENCES runs(id), used_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stream_deep_frames (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    observed INTEGER NOT NULL, retained INTEGER NOT NULL,
    PRIMARY KEY (run_id, worker, ordinal),
    FOREIGN KEY (run_id, worker, ordinal) REFERENCES stream_calls(run_id, worker, ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stream_deep_steps (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    step INTEGER NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (run_id, worker, ordinal, step),
    FOREIGN KEY (run_id, worker, ordinal) REFERENCES stream_deep_frames(run_id, worker, ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stream_boundary_budget (
    run_id TEXT PRIMARY KEY REFERENCES runs(id), used_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stream_boundaries (
    run_id TEXT NOT NULL REFERENCES runs(id), worker TEXT NOT NULL, ordinal INTEGER NOT NULL,
    payload BLOB NOT NULL, PRIMARY KEY (run_id, worker, ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS run_environment (
    run_id TEXT PRIMARY KEY REFERENCES runs(id), payload TEXT NOT NULL
);
-- Rule 8: the order tests started in, per worker, with the state of `random` at that
-- point (rule 7) so that a rerun can restore it.
CREATE TABLE IF NOT EXISTS run_tests (
    run_id TEXT NOT NULL, worker TEXT NOT NULL, position INTEGER NOT NULL,
    node_id TEXT NOT NULL, random_state TEXT,
    PRIMARY KEY (run_id, worker, position)
) WITHOUT ROWID;
-- Rule 7: other generator records for the run (numpy at import, pytest-randomly's seed).
CREATE TABLE IF NOT EXISTS run_rng (
    run_id TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
) WITHOUT ROWID;
-- Parts of a run's result that did not fit in the short .ldr/last.json, whole.
CREATE TABLE IF NOT EXISTS run_sections (
    run_id TEXT NOT NULL REFERENCES runs(id), name TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
) WITHOUT ROWID;
"""
VALUE_FIELDS = ("in", "out", "in_fp", "out_fp", "input_changes")


def recording_failure(path, error, calls, address):
    try:
        free = shutil.disk_usage(Path(path).parent).free
    except OSError:
        free = None
    return {
        "error": capture_error(error),
        "calls_recorded": calls,
        "last_address": address,
        "free_disk_bytes": free,
        "next_command": "ldr check_machine",
    }


def recording_failure_text(failure):
    free = failure["free_disk_bytes"]
    space = f"{free:,} bytes free" if free is not None else "free disk space unavailable"
    address = failure["last_address"] or "(no call recorded)"
    return (
        f"RECORDING INCOMPLETE: {failure['error']}. {failure['calls_recorded']:,} call(s) recorded. "
        f"Last address: {address}. Disk: {space}. Run {failure['next_command']}, resolve the write "
        "failure, then replay the original run command."
    )


def _journal_mode():
    # Older SQLite can corrupt a WAL database when writers race a checkpoint.
    # https://sqlite.org/wal.html#the_wal_reset_bug
    version = sqlite3.sqlite_version_info
    if version >= (3, 51, 3) or version[:2] == (3, 50) and version[2] >= 7:
        return "wal"
    if version[:2] == (3, 44) and version[2] >= 6:
        return "wal"
    return "delete"


def _json(value):
    return json.dumps(value, separators=(",", ":"))


def storage_settings(project_root, *, deep=False, boundary=False):
    path = Path(project_root) / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            settings = tomllib.load(handle)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        settings = {}
    for key in ("tool", "ldr", "storage"):
        settings = settings.get(key, {})
        if not isinstance(settings, dict):
            raise TypeError("[tool.ldr.storage] must be a table")
    batch_size = settings.get("batch_size", DEFAULT_BATCH_SIZE)
    busy_timeout = settings.get("busy_timeout", DEFAULT_BUSY_TIMEOUT)
    capture_lines = settings.get("capture_lines", False)
    first_calls = settings.get("first_calls", DEFAULT_FIRST_CALLS)
    last_calls = settings.get("last_calls", DEFAULT_LAST_CALLS)
    if type(first_calls) is not int or not 0 < first_calls < 2**31:
        raise ValueError("[tool.ldr.storage] first_calls must be a positive integer")
    if type(last_calls) is not int or not 0 <= last_calls < 2**31:
        raise ValueError("[tool.ldr.storage] last_calls must be a nonnegative integer")
    walk_ceiling = settings.get("walk_ceiling", DEFAULT_WALK_CEILING)
    if type(walk_ceiling) is not int or not 0 <= walk_ceiling < 2**31:
        raise ValueError("[tool.ldr.storage] walk_ceiling must be a nonnegative integer (0 = no ceiling)")
    for name, value in (
        ("batch_size", batch_size),
        ("busy_timeout", busy_timeout),
    ):
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f"[tool.ldr.storage] {name} must be a positive integer")
    if type(capture_lines) is not bool:
        raise TypeError("[tool.ldr.storage] capture_lines must be true or false")
    return batch_size, busy_timeout, capture_lines, deep, boundary, first_calls, last_calls, walk_ceiling or None


def _timeline(timeline):
    """``(observed, steps)`` from a deep timeline, or the tuple a child sent."""
    if isinstance(timeline, (tuple, list)):
        return timeline[0], timeline[1]
    return timeline.observed, timeline.steps


def _call_rows(roots):
    ordinal = 0

    def walk(nodes, parent):
        nonlocal ordinal
        for node in nodes:
            ordinal += 1
            current = ordinal
            payload = node.to_dict(include_children=False)
            values = _json({key: payload.pop(key) for key in VALUE_FIELDS}).encode()
            yield current, parent, _json(payload), sha256(values).digest(), values
            yield from walk(node.children, current)

    yield from walk(roots, None)


class TraceStore:
    def __init__(
        self,
        path,
        run_id,
        batch_size,
        busy_timeout,
        capture_lines=False,
        capture_deep=False,
        capture_boundaries=False,
        first_calls=DEFAULT_FIRST_CALLS,
        last_calls=DEFAULT_LAST_CALLS,
        reference_run=None,
        walk_ceiling=DEFAULT_WALK_CEILING,
        reference_roots=None,
    ):
        self.path = path
        self._comparison = None
        self._reference_selected = False
        self.run_id = run_id
        self.batch_size = batch_size
        self.busy_timeout = busy_timeout
        self.capture_lines = capture_lines
        self.capture_deep = capture_deep
        self.capture_boundaries = capture_boundaries
        self.first_calls = first_calls
        self.last_calls = last_calls
        # The run whose per-call fingerprints each live call is compared with.
        self.reference_run = reference_run
        # The tests of that run that this run is compared with (pytest), or None for all calls.
        self.reference_roots = reference_roots
        # Nodes a value may need before it is sketched instead of walked (None: no ceiling).
        self.walk_ceiling = walk_ceiling

    @classmethod
    def start(
        cls,
        directory,
        invocation,
        batch_size,
        busy_timeout,
        capture_lines=False,
        capture_deep=False,
        capture_boundaries=False,
        first_calls=DEFAULT_FIRST_CALLS,
        last_calls=DEFAULT_LAST_CALLS,
        walk_ceiling=DEFAULT_WALK_CEILING,
    ):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with (directory / ".gitignore").open("x", encoding="utf-8") as ignore:
                ignore.write("*\n")
        except FileExistsError:
            pass
        path = directory / "traces.sqlite3"
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        store = cls(
            str(path),
            uuid4().hex,
            batch_size,
            busy_timeout,
            capture_lines,
            capture_deep,
            capture_boundaries,
            first_calls,
            last_calls,
            walk_ceiling=walk_ceiling,
        )
        with closing(store._connect(allow_mode_change=True)) as connection:
            # Two invocations can race to set the journal mode on a new database.
            required_mode = _journal_mode()
            # Pages freed by deleting old runs can be given back to the file. This only
            # takes effect on a new database; an older one is converted by prune.
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            deadline = monotonic() + busy_timeout
            while True:
                try:
                    mode = connection.execute(
                        f"PRAGMA journal_mode = {required_mode}"
                    ).fetchone()
                    if mode != (required_mode,):
                        raise RuntimeError(
                            f"SQLite could not enable {required_mode} journal mode"
                        )
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        exc.sqlite_errorcode != sqlite3.SQLITE_BUSY
                        or monotonic() >= deadline
                    ):
                        raise
                    sleep(0.01)
            connection.executescript(SCHEMA)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for table in ("call_records", "call_records_stage"):
                    columns = connection.execute(f"PRAGMA table_info({table})")
                    if not any(column[1] == "verdict" for column in columns):
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN verdict TEXT")
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, NULL)",
                    (store.run_id, time_ns(), _json(invocation)),
                )
        return store

    def append_stream(self, worker, rows, fingerprints=(), sizes=()):
        """Commit one batch of fingerprints and all selected detail."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._append_fingerprints(connection, worker, fingerprints)
            connection.executemany(
                "INSERT INTO value_sizes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(self.run_id, worker, *size) for size in sizes],
            )
            connection.executemany(
                "INSERT INTO stream_calls VALUES (?, ?, ?, ?, ?, ?)",
                [(self.run_id, worker, ordinal, address, parent, payload)
                 for ordinal, address, parent, payload, _, _ in rows],
            )
            connection.executemany(
                "INSERT INTO stream_values VALUES (?, ?, ?, ?, ?, ?)",
                [(self.run_id, worker, ordinal, phase, name, blob)
                 for ordinal, _, _, _, values, _ in rows for phase, name, blob in values],
            )
            for ordinal, _, _, _, _, timeline in rows:
                if timeline is None:
                    continue
                observed, steps = _timeline(timeline)
                key = (self.run_id, worker, ordinal)
                connection.execute(
                    "INSERT INTO stream_deep_frames VALUES (?, ?, ?, ?, ?)",
                    (*key, observed, len(steps)),
                )
                connection.executemany(
                    "INSERT INTO stream_deep_steps VALUES (?, ?, ?, ?, ?)",
                    [(*key, index, data) for index, data in enumerate(steps, 1)],
                )
        if self._comparison is not None:
            self._comparison.record(fingerprints)
        return len(rows)

    def comparison_snapshot(self, run_id=None, roots=None, node_ids=None, replicated=False):
        """The writer fixes the reference and serves the same snapshot to every worker."""
        if not self._reference_selected:
            with closing(self._connect()) as connection:
                if node_ids is not None:
                    run_id, roots = previous.run_for_tests(
                        connection, self.run_id, node_ids, replicated=replicated
                    )
                if run_id is not None:
                    reference = previous.load(connection, run_id, roots)
                    row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
                    files = json.loads(row[0]).get("files") or {} if row and row[0] else {}
                    tests = previous.tests_of(connection, run_id) if node_ids is not None else None
                    self._comparison = previous.Comparison(reference, files, tests)
            self._reference_selected = True
        if self._comparison is None:
            return None
        reference = self._comparison.previous
        return reference.run_id, reference.table, reference.roots

    def comparison_result(self, files, stopped=None):
        if self._comparison is None:
            return None
        tests = set()
        if self._comparison.tests is not None:
            with closing(self._connect()) as connection:
                tests = previous.tests_of(connection, self.run_id)
        result, missing = self._comparison.result(files, tests, stopped)
        if not stopped:
            self.save_section("missing_calls", _json({name: sorted(addresses) for name, addresses in missing.items()}))
        return result

    def _append_fingerprints(self, connection, worker, fingerprints):
        """One packed record per call, and a lookup row per argument and return digest.

        The lookup answers "which call's argument equals another call's return"
        by fingerprint. Status words (unknown, redacted, ...) are not digests and
        are left out of it, so they never match each other.
        """
        records = []
        lookup = []
        run_id = self.run_id
        for ordinal, frame_id, parent, function, error, parts, reference, signature, verdict in fingerprints:
            records.append(
                (run_id, worker, ordinal, frame_id, parent, function, error, reference,
                 signature, marshal.dumps(parts), verdict)
            )
            lookup.extend(
                (run_id, fp, worker, ordinal, role, name)
                for role, name, fp in parts
                if role != "out" and is_digest(fp) and not fp.endswith("~")  # a sketch is not a value
            )
        connection.executemany(
            "INSERT INTO call_records_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records
        )
        connection.executemany("INSERT INTO call_lookup_stage VALUES (?, ?, ?, ?, ?, ?)", lookup)

    def build_lookup(self):
        """Move this run's staged records into the indexed tables, in index order.

        Inserting in key order into an indexed table is much cheaper than
        keeping the indexes current on every batch while the program runs.
        """
        with closing(self._connect()) as connection, connection:
            # The two sorts and the index pages stay in memory instead of temporary files.
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA cache_size = -131072")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO call_records SELECT * FROM call_records_stage "
                "WHERE run_id = ? ORDER BY worker, ordinal",
                (self.run_id,),
            )
            connection.execute(
                "INSERT INTO call_lookup SELECT * FROM call_lookup_stage "
                "WHERE run_id = ? ORDER BY fp",
                (self.run_id,),
            )
            connection.execute("DELETE FROM call_records_stage WHERE run_id = ?", (self.run_id,))
            connection.execute("DELETE FROM call_lookup_stage WHERE run_id = ?", (self.run_id,))

    def deep_totals(self):
        with closing(self._connect()) as connection:
            stored, retained = connection.execute(
                "SELECT count(*), coalesce(sum(retained), 0) FROM stream_deep_frames WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()
            (size,) = connection.execute(
                "SELECT COALESCE(SUM(length(payload)), 0) FROM stream_deep_steps WHERE run_id = ?", (self.run_id,)
            ).fetchone()
        return {"frames": stored, "retained": retained, "bytes": size}

    def append_boundaries(self, worker, rows):
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT INTO stream_boundaries VALUES (?, ?, ?, ?)",
                [(self.run_id, worker, ordinal, payload) for ordinal, payload in rows],
            )
        return len(rows)

    def boundary_bytes_used(self):
        with closing(self._connect()) as connection:
            (used,) = connection.execute(
                "SELECT COALESCE(SUM(length(payload)), 0) FROM stream_boundaries WHERE run_id = ?", (self.run_id,)
            ).fetchone()
        return used

    @staticmethod
    def _find_run(connection, run_id):
        return (
            connection.execute(
                # A rerun that did not match the run it repeats is never "the latest run".
                "SELECT id FROM runs WHERE status IS NULL "
                "OR COALESCE(json_extract(status, '$.rerun.verified'), 1) != 0 "
                "ORDER BY started_ns DESC, id DESC LIMIT 1"
            ).fetchone()
            if run_id is None
            else connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        )

    @staticmethod
    def read_boundaries(directory, run_id, *, offset, limit):
        path = Path(directory, "traces.sqlite3").resolve().as_uri()
        with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as connection:
            row = TraceStore._find_run(connection, run_id)
            if row is None:
                return None, []
            run_id = row[0]
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stream_boundaries'"
            ).fetchone() is None:
                return run_id, []
            rows = connection.execute(
                "SELECT worker, ordinal, payload FROM stream_boundaries WHERE run_id = ? "
                "ORDER BY worker, ordinal LIMIT ? OFFSET ?", (run_id, limit, offset)
            )
            return run_id, [
                {"worker": worker, "ordinal": ordinal, **json.loads(payload)}
                for worker, ordinal, payload in rows
            ]

    def save_environment(self, record):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO run_environment VALUES (?, ?)",
                (self.run_id, _json(record)),
            )

    def save_tests(self, worker, rows):
        """(position, node id, random state) for each test that started, in start order."""
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR REPLACE INTO run_tests VALUES (?, ?, ?, ?, ?)",
                [(self.run_id, worker, position, node, state) for position, node, state in rows],
            )

    def load_tests(self, run_id):
        """[(worker, position, node id, random state)], by worker then position."""
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT worker, position, node_id, random_state FROM run_tests "
                "WHERE run_id = ? ORDER BY worker, position",
                (run_id,),
            ).fetchall()

    def save_rng(self, name, payload):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO run_rng VALUES (?, ?, ?)", (self.run_id, name, payload)
            )

    def list_rng(self, run_id):
        with closing(self._connect()) as connection:
            return [
                name for (name,) in connection.execute(
                    "SELECT name FROM run_rng WHERE run_id = ? ORDER BY name", (run_id,)
                )
            ]

    def load_rng(self, run_id, name):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM run_rng WHERE run_id = ? AND name = ?", (run_id, name)
            ).fetchone()
        return row[0] if row else None

    def save_section(self, name, text):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO run_sections VALUES (?, ?, ?)", (self.run_id, name, text)
            )

    def load_section(self, run_id, name):
        """One stored part of a run's result (see save_section), or None."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM run_sections WHERE run_id = ? AND name = ?", (run_id, name)
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def read_section(directory, run_id, name):
        """One stored part of a run's result, or None. Read-only."""
        path = Path(directory, "traces.sqlite3").resolve().as_uri()
        with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_sections'"
            ).fetchone() is None:
                return None
            row = connection.execute(
                "SELECT payload FROM run_sections WHERE run_id = ? AND name = ?", (run_id, name)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def load_environment(self, run_id):
        """One run's environment record, or None when it was not saved."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM run_environment WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def read_environment(directory, run_id):
        """Read-only lookup for ``ldr show``: (run id, record or None)."""
        path = Path(directory, "traces.sqlite3").resolve().as_uri()
        with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as connection:
            row = TraceStore._find_run(connection, run_id)
            if row is None:
                return None, None
            run_id = row[0]
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_environment'"
            ).fetchone() is None:
                return run_id, None
            saved = connection.execute(
                "SELECT payload FROM run_environment WHERE run_id = ?", (run_id,)
            ).fetchone()
            return run_id, json.loads(saved[0]) if saved else None

    @staticmethod
    def read_call(directory, run_id, address):
        """One call of a run: ``("kept", detail)``, ``("not kept", None)`` or ``("unknown", None)``."""
        path = Path(directory, "traces.sqlite3").resolve().as_uri()
        with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as connection:
            kept = connection.execute(
                "SELECT payload FROM stream_calls WHERE run_id = ? AND frame_id = ?", (run_id, address)
            ).fetchone()
            if kept is not None:
                return "kept", json.loads(kept[0])
            known = connection.execute(
                "SELECT 1 FROM call_records WHERE run_id = ? AND frame_id = ?", (run_id, address)
            ).fetchone()
        return ("not kept", None) if known else ("unknown", None)

    @staticmethod
    def read_deep(directory, run_id, frame_id=None, *, offset=0, limit=20):
        path = Path(directory, "traces.sqlite3").resolve().as_uri()
        with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as connection:
            row = TraceStore._find_run(connection, run_id)
            if row is None:
                return None, None if frame_id is not None else []
            run_id = row[0]
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stream_deep_frames'"
            ).fetchone() is None:
                return run_id, None if frame_id is not None else []
            query = (
                "SELECT c.frame_id, d.worker, d.ordinal, d.observed, d.retained "
                "FROM stream_deep_frames AS d "
                "JOIN stream_calls AS c USING (run_id, worker, ordinal) WHERE d.run_id = ?"
            )
            if frame_id is None:
                rows = connection.execute(
                    query + " ORDER BY c.frame_id LIMIT ? OFFSET ?", (run_id, limit, offset)
                )
                return run_id, [
                    {"frame_id": frame, "observed": observed, "retained": retained}
                    for frame, _, _, observed, retained in rows
                ]
            row = connection.execute(
                query + " AND c.frame_id = ? LIMIT 1", (run_id, frame_id)
            ).fetchone()
            if row is None:
                return run_id, None
            _, worker, ordinal, observed, retained = row
            steps = connection.execute(
                "SELECT step, payload FROM stream_deep_steps "
                "WHERE run_id = ? AND worker = ? AND ordinal = ? AND step > ? "
                "ORDER BY step LIMIT ?",
                (run_id, worker, ordinal, offset, limit),
            )
            return run_id, {
                "frame_id": frame_id,
                "observed": observed,
                "retained": retained,
                "steps": [{"step": step, **json.loads(data)} for step, data in steps],
            }

    def frame_values(self, run_id, frame_id):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT worker, ordinal, payload FROM stream_calls WHERE run_id = ? AND frame_id = ? LIMIT 1",
                (run_id, frame_id),
            ).fetchone()
            if row is None:
                return {}
            worker, ordinal, payload = row
            values = json.loads(payload).get("values", {})
            if not values:
                return {}
            for phase, name, data in connection.execute(
                "SELECT phase, name, payload FROM stream_values WHERE run_id = ? AND worker = ? AND ordinal = ?",
                (run_id, worker, ordinal),
            ):
                values[phase][name].update(status="complete", value=json.loads(data))
            return values

    def value_bytes_used(self):
        with closing(self._connect()) as connection:
            (used,) = connection.execute(
                "SELECT COALESCE(SUM(length(payload)), 0) FROM stream_values WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()
        return used

    def finish_stream(self, worker, summary):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO stream_summary VALUES (?, ?, ?)",
                (self.run_id, worker, _json(summary)),
            )

    def read_stream(self, run_id):
        with closing(self._connect()) as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT summary FROM stream_summary WHERE run_id = ? ORDER BY worker",
                    (run_id,),
                )
            ]

    def _connect(self, *, allow_mode_change=False):
        connection = sqlite3.connect(
            Path(self.path).as_uri() + "?mode=rw", uri=True, timeout=self.busy_timeout
        )
        connection.execute("PRAGMA foreign_keys = ON")
        if (
            not allow_mode_change
            and _journal_mode() != "wal"
            and connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        ):
            connection.close()
            raise sqlite3.OperationalError(
                "this SQLite runtime cannot safely write the WAL database"
            )
        return connection

    def save_worker(self, worker, roots, summary):
        self.save_worker_rows(worker, _call_rows(roots), summary)

    def save_worker_rows(self, worker, rows, summary):
        rows = iter(rows)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO workers VALUES (?, ?, NULL)", (self.run_id, worker)
                )
            while batch := list(islice(rows, self.batch_size)):
                with connection:
                    connection.executemany(
                        "INSERT INTO captured_values VALUES (?, ?) ON CONFLICT DO NOTHING",
                        (
                            (fingerprint, values)
                            for _, _, _, fingerprint, values in batch
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            (self.run_id, worker, ordinal, parent, payload, fingerprint)
                            for ordinal, parent, payload, fingerprint, _ in batch
                        ),
                    )
            with connection:
                connection.execute(
                    "UPDATE workers SET summary = ? WHERE run_id = ? AND name = ?",
                    (_json(summary), self.run_id, worker),
                )

    def load_worker(self, worker):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT summary FROM workers WHERE run_id = ? AND name = ?",
                (self.run_id, worker),
            ).fetchone()
            if row is None or row[0] is None:
                return None
            roots = []
            nodes = {}
            for ordinal, parent, payload, values in connection.execute(
                "SELECT c.ordinal, c.parent, c.payload, v.payload FROM calls AS c "
                "JOIN captured_values AS v ON v.fingerprint = c.values_fingerprint "
                "WHERE c.run_id = ? AND c.worker = ? ORDER BY c.ordinal",
                (self.run_id, worker),
            ):
                node = Node.from_dict(json.loads(payload) | json.loads(values))
                nodes[ordinal] = node
                (roots if parent is None else nodes[parent].children).append(node)
            return roots, json.loads(row[0])

    def settings(self):
        """Constructor arguments, so another process can open the same run."""
        # Attributes a run sets after opening the store are not constructor arguments.
        later = ("full_tests", "reference_prefix", "uncap")
        return {
            key: value for key, value in vars(self).items()
            if not key.startswith("_") and key not in later
        }

    def close(self):
        """Nothing to wait for: this store writes directly."""

    def finish(self, status, *, build=True):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE runs SET status = ? WHERE id = ?", (_json(status), self.run_id)
            )
        if build:
            self.build_lookup()
