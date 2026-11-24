"""Batch 1, step 2: every run is compared with the previous run of the same target.

pytest runs match by test node id, scripts by exact command. The comparison is made through
SQLite (a join over the two runs' rows), the note "also varied across N runs with no edit"
follows from identical file hashes, and only the last 2 runs of a target are kept."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from ldr.sqlite_store import TraceStore

REPO = Path(__file__).resolve().parents[1]

TEMPLATE = """\
import os


def f(n):
    return n * 2 + ({delta} if n == 12 else 0)


def pid():
    return os.getpid()


for index in range(30):
    f(index + 1)
{extra}
"""


def ldr(project, *args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def write(project, *, delta=0, extra=""):
    (project / "app.py").write_text(TEMPLATE.format(delta=delta, extra=extra), encoding="utf-8")


def report(project):
    return json.loads((project / ".ldr" / "last.json").read_bytes())


def runs(project):
    with closing(sqlite3.connect(project / ".ldr" / "traces.sqlite3")) as connection:
        return [row[0] for row in connection.execute("SELECT id FROM runs ORDER BY started_ns")]


def test_the_first_run_of_a_target_says_there_is_no_previous_run(tmp_path):
    write(tmp_path)
    result = ldr(tmp_path, "run", "app.py")
    assert "PREVIOUS RUN: none" in result.stdout
    assert "reference" not in report(tmp_path)["status"]


def test_a_run_is_compared_with_the_previous_run_of_the_same_command(tmp_path):
    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    first = report(tmp_path)["status"]["run_id"]
    write(tmp_path, delta=1)
    result = ldr(tmp_path, "run", "app.py")
    data = report(tmp_path)
    reference = data["status"]["reference"]
    assert reference["run_id"] == first
    assert (reference["compared"], reference["same"], reference["different"]) == (30, 29, 1)
    assert reference["changed"] == ["app.py:f"]
    assert reference["changed_detail"] == {"app.py:f": [1, 0]}  # the file was edited: no note
    assert data["behavior"]["changed"] and data["behavior"]["stream_changed_functions"] == ["app.py:f"]
    assert "PREVIOUS RUN" in result.stdout and "app.py:f" in result.stdout
    assert "no edit" not in result.stdout
    # The differing call kept its detail without a rerun.
    assert len(runs(tmp_path)) == 2
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        kept = connection.execute(
            "SELECT 1 FROM stream_calls WHERE run_id = ? AND frame_id = 'app.py:f#12'",
            (data["status"]["run_id"],),
        ).fetchone()
    assert kept is not None


def test_a_value_that_differs_between_runs_of_identical_files_carries_the_note(tmp_path):
    write(tmp_path, extra="pid()")
    ldr(tmp_path, "run", "app.py")
    ldr(tmp_path, "run", "app.py")
    reference = report(tmp_path)["status"]["reference"]
    assert reference["changed"] == ["app.py:pid"]
    assert reference["changed_detail"] == {"app.py:pid": [1, 1]}
    assert reference["files_unchanged"] is True
    result = (tmp_path / ".ldr" / "last.txt").read_text(encoding="utf-8")
    assert "also varied across 2 runs with no edit" in result


def test_an_edit_between_the_runs_removes_the_note(tmp_path):
    write(tmp_path, extra="pid()")
    ldr(tmp_path, "run", "app.py")
    write(tmp_path, extra="pid()\n# edited")
    ldr(tmp_path, "run", "app.py")
    reference = report(tmp_path)["status"]["reference"]
    assert reference["changed_detail"] == {"app.py:pid": [1, 0]}
    assert reference["files_unchanged"] is False


def test_another_command_is_another_target(tmp_path):
    write(tmp_path)
    ldr(tmp_path, "run", "app.py", "one")
    result = ldr(tmp_path, "run", "app.py", "two")
    assert "PREVIOUS RUN: none" in result.stdout
    (tmp_path / "extra").mkdir()
    result = ldr(tmp_path, "run", "app.py", "one", "--only", "extra", "app.py")
    assert "PREVIOUS RUN: none" in result.stdout  # --only is part of the command
    result = ldr(tmp_path, "run", "app.py", "one")
    assert report(tmp_path)["status"]["reference"]["compared"] == 30


TEST_A = "def test_a1():\n    assert 1 + 1 == 2\n\n\ndef test_a2():\n    assert 2 * 2 == 4\n"
TEST_B = "def test_b1():\n    assert 3 - 1 == 2\n"


def test_pytest_runs_match_by_node_id_and_list_the_tests_not_compared(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(TEST_A, encoding="utf-8")
    (tmp_path / "tests" / "test_b.py").write_text(TEST_B, encoding="utf-8")
    ldr(tmp_path, "pytest", "tests/test_a.py")
    first = report(tmp_path)["status"]["run_id"]
    result = ldr(tmp_path, "pytest", "tests")
    second = report(tmp_path)["status"]
    reference = second["reference"]
    assert reference["run_id"] == first
    assert reference["tests"]["compared"] == 2
    assert reference["tests"]["only_in_this_run"] == ["tests/test_b.py::test_b1"]
    assert reference["tests"]["only_in_previous"] == []
    assert reference["different"] == 0 and reference["compared"] > 0
    assert "NOT COMPARED" in result.stdout and "test_b1" in result.stdout
    # tests/ then tests/test_b.py: the latest earlier run that shares a test is the second.
    ldr(tmp_path, "pytest", "tests/test_b.py")
    third = report(tmp_path)["status"]["reference"]
    assert third["run_id"] == second["run_id"]
    assert third["tests"]["compared"] == 1
    assert sorted(third["tests"]["only_in_previous"]) == [
        "tests/test_a.py::test_a1", "tests/test_a.py::test_a2",
    ]


def test_only_the_last_two_runs_of_a_target_are_kept(tmp_path):
    write(tmp_path)
    for _ in range(4):
        ldr(tmp_path, "run", "app.py")
    kept = runs(tmp_path)
    assert len(kept) == 2
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        assert {row[0] for row in connection.execute("SELECT DISTINCT run_id FROM call_records")} == set(kept)
        assert {row[0] for row in connection.execute("SELECT DISTINCT run_id FROM value_sizes")} <= set(kept)
    ldr(tmp_path, "run", "app.py", "other")  # another target keeps its own runs
    assert len(runs(tmp_path)) == 3


def test_the_number_of_runs_kept_is_a_setting(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ldr.storage]\nkeep_runs = 3\n", encoding="utf-8")
    write(tmp_path)
    for _ in range(5):
        ldr(tmp_path, "run", "app.py")
    assert len(runs(tmp_path)) == 3


def test_the_previous_run_is_never_held_in_memory():
    assert not hasattr(TraceStore, "read_reference")


def test_the_lookup_of_one_call_uses_the_index_and_does_not_scan_the_previous_run(tmp_path):
    # Without the index a lookup reads every call of the run: a second run of a large program
    # took 12 times as long as the first.
    from ldr.previous import Lookup

    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        run, address = connection.execute("SELECT run_id, frame_id FROM call_records LIMIT 1").fetchone()
        plan = " ".join(
            row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + Lookup.QUERY, (run, address))
        )
    assert "call_records_frame" in plan and "SCAN" not in plan, plan
    assert "frame_id=?" in plan, plan  # both equalities are used: the search is by address, not by run


def test_a_lookup_answers_with_the_signature_of_the_call_at_that_address(tmp_path):
    from ldr.previous import Lookup

    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        run, address, signature = connection.execute("SELECT run_id, frame_id, signature FROM call_records LIMIT 1").fetchone()
    lookup = Lookup(str(tmp_path / ".ldr" / "traces.sqlite3"), run)
    try:
        assert lookup.signature(address) == bytes(signature)
        assert lookup.signature(address + "x") is None
    finally:
        lookup.close()
