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
from ldr import processes

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
    result = processes.run(
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


THREADED = """\
import threading


def work(n):
    return n * 2 + (7 if n == 12 else 0)


def run():
    for index in range(30):
        work(index + 1)


thread = threading.Thread(target=run)
thread.start()
thread.join()
"""


def test_a_program_that_calls_from_another_thread_still_looks_its_calls_up(tmp_path):
    # The lookup's connection is made by the thread that starts the recording; the calls are
    # made by another. It used to fail on the first call and give up for the rest of the run.
    (tmp_path / "app.py").write_text(THREADED, encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    ldr(tmp_path, "run", "app.py")
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        summaries = [json.loads(row[0]) for row in connection.execute(
            "SELECT s.summary FROM stream_summary s JOIN runs r ON r.id = s.run_id ORDER BY r.started_ns"
        )]
    assert len(summaries) == 2
    assert "previous_error" not in summaries[-1], summaries[-1].get("previous_error")
    assert report(tmp_path)["status"]["reference"]["compared"] >= 30


# ---- Hotfix step 2: the previous run is loaded once, into a dict, and never asked per call ----

LIB = """\
def work(n):
    return n * 2 + ({delta} if n == 12 else 0)


def many():
    for index in range(30):
        work(index + 1)
"""

SELECTS = ("SELECT", "WITH")


def statements(connection):
    seen = []
    connection.set_trace_callback(seen.append)
    return seen


def test_the_previous_run_is_loaded_with_one_query_and_then_answers_without_sql(tmp_path):
    from ldr import previous

    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    path = tmp_path / ".ldr" / "traces.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        run, address, signature = connection.execute(
            "SELECT run_id, frame_id, signature FROM call_records LIMIT 1"
        ).fetchone()
        seen = statements(connection)
        loaded = previous.load(connection, run)
        selects = [text for text in seen if text.lstrip().upper().startswith(SELECTS)]
        assert len(selects) == 1 and "call_records" in selects[0], seen
        seen.clear()
        for _ in range(1000):
            assert loaded.signature(address) == bytes(signature)
            assert loaded.signature(address + "x") is None
        assert seen == []  # no statement runs per call
    assert len(loaded) > 0
    assert not hasattr(previous, "Lookup")  # the per-call lookup is gone


def test_the_load_can_be_limited_to_the_tests_two_runs_share(tmp_path):
    from ldr import previous

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(TEST_A, encoding="utf-8")
    (tmp_path / "tests" / "test_b.py").write_text(TEST_B, encoding="utf-8")
    ldr(tmp_path, "pytest", "tests")
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        run = connection.execute("SELECT id FROM runs").fetchone()[0]
        everything = previous.load(connection, run)
        only_b = previous.load(connection, run, roots=[previous.encode_node_id("tests/test_b.py::test_b1")])
    assert 0 < len(only_b) < len(everything)
    assert all(address.startswith("tests%2Ftest_b.py") or "test_b1" in address for address in only_b.addresses())


def test_a_pytest_run_looks_up_the_run_that_shares_its_test_ids_not_the_latest_run(tmp_path):
    (tmp_path / "lib.py").write_text(LIB.format(delta=0), encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("from lib import many\n\n\ndef test_a1():\n    many()\n", encoding="utf-8")
    (tests / "test_b.py").write_text("from lib import many\n\n\ndef test_b1():\n    many()\n", encoding="utf-8")
    ldr(tmp_path, "pytest", "tests/test_a.py", "tests/test_b.py")
    first = report(tmp_path)["status"]["run_id"]
    ldr(tmp_path, "pytest", "tests/test_a.py")  # the latest run: it shares no test with the next one
    (tmp_path / "lib.py").write_text(LIB.format(delta=1), encoding="utf-8")
    ldr(tmp_path, "pytest", "tests/test_b.py")
    status = report(tmp_path)["status"]
    assert status["reference"]["run_id"] == first
    # work(12) differs from the run that shares the test, and the lookup found it while recording:
    # it is beyond the first calls of the function yet it kept its detail.
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        kept = connection.execute(
            "SELECT 1 FROM stream_calls WHERE run_id = ? AND frame_id LIKE '%lib.py:work#12'", (status["run_id"],)
        ).fetchone()
    assert kept is not None


def test_a_script_loads_its_previous_run_when_it_starts(tmp_path):
    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    write(tmp_path, delta=1)
    ldr(tmp_path, "run", "app.py")
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        summary = json.loads(connection.execute(
            "SELECT s.summary FROM stream_summary s JOIN runs r ON r.id = s.run_id ORDER BY r.started_ns DESC LIMIT 1"
        ).fetchone()[0])
    assert summary["previous_loaded"] == 30 and "previous_error" not in summary
