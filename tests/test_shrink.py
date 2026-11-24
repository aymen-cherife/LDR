"""Batch 2, step 3: deleting old runs gives the disk space back. The deleted rows are removed
and the file gets smaller (incremental auto-vacuum on new databases, one VACUUM that converts an
old one)."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from ldr import previous
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

APP = """\
class Box:
    def __init__(self, n):
        self.items = list(range(n))
        self.name = "box" + str(n)

    def total(self, extra):
        return sum(self.items) + len(extra)


for index in range(1500):
    Box(40 + index % 7).total([index, index + 1])
"""


def ldr(project, *args):
    result = processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def database(project):
    return project / ".ldr" / "traces.sqlite3"


def setup(project, *, runs=3):
    (project / "pyproject.toml").write_text("[tool.ldr.storage]\nkeep_runs = 5\n", encoding="utf-8")
    (project / "app.py").write_text(APP, encoding="utf-8")
    for _ in range(runs):
        ldr(project, "run", "app.py")


def count(project, table):
    with closing(sqlite3.connect(database(project))) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_new_database_frees_deleted_pages(tmp_path):
    setup(tmp_path, runs=1)
    with closing(sqlite3.connect(database(tmp_path))) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (2,)  # incremental


def test_the_file_is_smaller_after_old_runs_are_pruned(tmp_path):
    setup(tmp_path)
    before = os.path.getsize(database(tmp_path))
    deleted = previous.prune(str(tmp_path / ".ldr"), 1)
    assert len(deleted) == 2
    assert count(tmp_path, "runs") == 1
    assert os.path.getsize(database(tmp_path)) < before * 0.6, (before, os.path.getsize(database(tmp_path)))
    assert not Path(str(database(tmp_path)) + "-wal").exists() or os.path.getsize(str(database(tmp_path)) + "-wal") == 0


def test_the_deleted_rows_are_gone_from_every_table(tmp_path):
    setup(tmp_path)
    kept_before = count(tmp_path, "call_records")
    previous.prune(str(tmp_path / ".ldr"), 1)
    with closing(sqlite3.connect(database(tmp_path))) as connection:
        (kept,) = connection.execute("SELECT id FROM runs").fetchone()
        for table in previous._TABLES:
            others = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id != ?", (kept,)).fetchone()[0]
            assert others == 0, table
    assert 0 < count(tmp_path, "call_records") < kept_before


def test_an_old_database_without_auto_vacuum_is_converted_and_shrinks(tmp_path):
    setup(tmp_path)
    with closing(sqlite3.connect(database(tmp_path))) as connection:
        connection.execute("PRAGMA auto_vacuum = NONE")
        connection.execute("VACUUM")  # what a database made before this change looks like
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (0,)
    before = os.path.getsize(database(tmp_path))
    previous.prune(str(tmp_path / ".ldr"), 1)
    assert os.path.getsize(database(tmp_path)) < before * 0.6
    with closing(sqlite3.connect(database(tmp_path))) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (2,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_nothing_is_vacuumed_when_nothing_was_deleted(tmp_path):
    setup(tmp_path, runs=2)
    before = os.path.getsize(database(tmp_path))
    assert previous.prune(str(tmp_path / ".ldr"), 5) == []
    assert os.path.getsize(database(tmp_path)) == before


def test_prune_removes_only_finished_run_directories_for_deleted_rows(tmp_path):
    from ldr import supervisor
    from ldr.sqlite_store import SCHEMA
    import json

    ldr_dir = tmp_path / ".ldr"
    for name, state in (("old", "finished"), ("live", "running"), ("kept", "finished")):
        directory = ldr_dir / "runs" / name
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"state": state, "store_run_id": name}))
    with closing(sqlite3.connect(database(tmp_path))) as connection, connection:
        connection.executescript(SCHEMA)
        invocation = json.dumps({"runner": "run", "target": "app.py", "arguments": []})
        connection.executemany("INSERT INTO runs VALUES (?, ?, ?, ?)", [
            ("old", 1, invocation, "{}"), ("live", 2, invocation, "{}"), ("kept", 3, invocation, "{}"),
        ])

    assert previous.prune(str(ldr_dir), 1) == ["old"]
    assert not (ldr_dir / "runs" / "old").exists()
    assert (ldr_dir / "runs" / "live").exists()
    assert (ldr_dir / "runs" / "kept").exists()
    assert not hasattr(supervisor, "KEEP_RUNS")
