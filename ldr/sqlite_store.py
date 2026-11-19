"""SQLite persistence for bounded, already-captured trace trees."""

import json
import os
import sqlite3
import tomllib
from contextlib import closing
from hashlib import sha256
from itertools import islice
from pathlib import Path
from time import monotonic, sleep, time_ns
from uuid import uuid4

from .node import Node

DEFAULT_BATCH_SIZE = 1000
DEFAULT_BUSY_TIMEOUT = 30
DEFAULT_TRACE_BYTES = 16 * 1024 * 1024
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
"""
VALUE_FIELDS = ("in", "out", "in_fp", "out_fp", "input_changes")


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


def storage_settings(project_root):
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
    max_trace_bytes = settings.get("max_trace_bytes", DEFAULT_TRACE_BYTES)
    capture_lines = settings.get("capture_lines", False)
    max_value_bytes = settings.get("max_value_bytes", 0)
    for name, value in (
        ("batch_size", batch_size),
        ("busy_timeout", busy_timeout),
        ("max_trace_bytes", max_trace_bytes),
    ):
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f"[tool.ldr.storage] {name} must be a positive integer")
    if type(capture_lines) is not bool:
        raise TypeError("[tool.ldr.storage] capture_lines must be true or false")
    if capture_lines and "max_trace_bytes" not in settings:
        raise ValueError(
            "[tool.ldr.storage] capture_lines requires an explicit max_trace_bytes"
        )
    if type(max_value_bytes) is not int or not 0 <= max_value_bytes < 2**63:
        raise ValueError(
            "[tool.ldr.storage] max_value_bytes must be a nonnegative integer"
        )
    if max_value_bytes and (
        "max_trace_bytes" not in settings or max_value_bytes > max_trace_bytes
    ):
        raise ValueError(
            "[tool.ldr.storage] max_value_bytes requires an explicit max_trace_bytes at least as large"
        )
    return batch_size, busy_timeout, max_trace_bytes, capture_lines, max_value_bytes


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
        max_trace_bytes=DEFAULT_TRACE_BYTES,
        capture_lines=False,
        max_value_bytes=0,
    ):
        self.path = path
        self.run_id = run_id
        self.batch_size = batch_size
        self.busy_timeout = busy_timeout
        self.max_trace_bytes = max_trace_bytes
        self.capture_lines = capture_lines
        self.max_value_bytes = max_value_bytes

    @classmethod
    def start(
        cls,
        directory,
        invocation,
        batch_size,
        busy_timeout,
        max_trace_bytes=DEFAULT_TRACE_BYTES,
        capture_lines=False,
        max_value_bytes=0,
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
            max_trace_bytes,
            capture_lines,
            max_value_bytes,
        )
        with closing(store._connect(allow_mode_change=True)) as connection:
            # Two invocations can race to set the journal mode on a new database.
            required_mode = _journal_mode()
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
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, NULL)",
                    (store.run_id, time_ns(), _json(invocation)),
                )
                connection.execute(
                    "INSERT INTO stream_budget (run_id) VALUES (?)", (store.run_id,)
                )
                if store.max_value_bytes:
                    connection.execute(
                        "INSERT INTO stream_value_budget (run_id) VALUES (?)",
                        (store.run_id,),
                    )
        return store

    def append_stream(self, worker, rows):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            (used,) = connection.execute(
                "SELECT used_bytes FROM stream_budget WHERE run_id = ?", (self.run_id,)
            ).fetchone()
            admitted = []
            for ordinal, frame_id, parent, payload, _ in rows:
                size = len(payload) + len(frame_id.encode("utf-8", "surrogatepass"))
                if used + size > self.max_trace_bytes:
                    break
                used += size
                admitted.append(
                    (self.run_id, worker, ordinal, frame_id, parent, payload)
                )
            connection.executemany(
                "INSERT INTO stream_calls VALUES (?, ?, ?, ?, ?, ?)", admitted
            )
            value_used = 0
            if self.max_value_bytes:
                (value_used,) = connection.execute(
                    "SELECT used_bytes FROM stream_value_budget WHERE run_id = ?",
                    (self.run_id,),
                ).fetchone()
                for ordinal, _, _, _, values in rows[: len(admitted)]:
                    for phase, name, data in values:
                        if (
                            value_used + len(data) <= self.max_value_bytes
                            and used + len(data) <= self.max_trace_bytes
                        ):
                            connection.execute(
                                "INSERT INTO stream_values VALUES (?, ?, ?, ?, ?, ?)",
                                (self.run_id, worker, ordinal, phase, name, data),
                            )
                            used += len(data)
                            value_used += len(data)
                connection.execute(
                    "UPDATE stream_value_budget SET used_bytes = ? WHERE run_id = ?",
                    (value_used, self.run_id),
                )
            connection.execute(
                "UPDATE stream_budget SET used_bytes = ? WHERE run_id = ?",
                (used, self.run_id),
            )
        return len(admitted), value_used

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
                "SELECT used_bytes FROM stream_value_budget WHERE run_id = ?",
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
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO workers VALUES (?, ?, NULL)", (self.run_id, worker)
                )
            rows = _call_rows(roots)
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

    def finish(self, status):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE runs SET status = ? WHERE id = ?", (_json(status), self.run_id)
            )
