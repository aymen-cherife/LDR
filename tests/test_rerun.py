"""Phase 2 rules 9 to 11: rerun one test for detail, and verify it."""

import json
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

from ldr import rerun

REPO = Path(__file__).resolve().parents[1]

APP = """\
import random
import time


def work(n):
    return n * 2


def shuffled(names):
    return list({name for name in names})


def lucky():
    return random.randint(0, 10**9)


def now():
    return time.time_ns()
"""

TESTS = """\
import os
import app


def test_steady():
    for index in range(30):
        app.work(index)
    app.shuffled(["alpha", "beta", "gamma", "delta", "omega"])
    app.lucky()


def test_clock():
    for index in range(20):
        app.work(index)
    app.work(app.now())


def test_after():
    app.work(1)


def test_flaky():
    marker = os.path.join(os.path.dirname(__file__), "seen")
    first = not os.path.exists(marker)
    open(marker, "w").close()
    for index in range(12):
        app.work(index)
    assert not first
"""


def ldr(project, *args, expected=None):
    environment = dict(os.environ, PYTHONPATH=str(REPO))
    environment.pop("PYTHONHASHSEED", None)
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args], cwd=project, env=environment,
        capture_output=True, text=True, timeout=300,
    )
    if expected is not None:
        assert result.returncode == expected, result.stdout + result.stderr
    return result


def project(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "test_app.py").write_text(TESTS, encoding="utf-8")
    return tmp_path


def database(path):
    return sqlite3.connect(path / ".ldr" / "traces.sqlite3")


def test_the_address_is_split_into_its_test_and_node_id():
    assert rerun.test_of("tests/test_a.py::test_x[1%2F2]/app.py:f#3") == (
        "tests/test_a.py::test_x[1%2F2]", "tests/test_a.py::test_x[1/2]",
    )
    assert rerun.test_of("app.py:f#1") is None


def test_a_call_without_detail_is_rerun_verified_and_then_shown(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", expected=0)
    original = json.loads((tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8"))
    address = "test_app.py::test_steady/app.py:work#20"
    with database(tmp_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM stream_calls WHERE frame_id = ?", (address,)
        ).fetchone() == (0,)  # beyond the first 8 calls: no detail kept
    result = ldr(tmp_path, "run", "--detail", address, expected=0)
    assert "RERUN VERIFIED (alone)" in result.stdout, result.stdout + result.stderr
    assert " matched, 0 differ, 0 new, 0 missing" in result.stdout
    with database(tmp_path) as connection:
        run_id, status = connection.execute(
            "SELECT id, status FROM runs ORDER BY started_ns DESC LIMIT 1"
        ).fetchone()
        assert json.loads(status)["rerun"]["verified"] is True
        assert connection.execute(
            "SELECT COUNT(*) FROM stream_calls WHERE run_id = ? AND frame_id = ?", (run_id, address)
        ).fetchone() == (1,)
    # The run it repeats keeps its own result file.
    assert json.loads((tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8"))["status"]["run_id"] == (
        original["status"]["run_id"]
    )
    shown = ldr(tmp_path, "show", address, expected=0)
    assert address in shown.stdout and "in  n = 19" in shown.stdout and "returned 38" in shown.stdout


def test_detail_that_is_already_stored_is_not_rerun(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", expected=0)
    result = ldr(tmp_path, "run", "--detail", "test_app.py::test_steady/app.py:work#2", expected=0)
    assert "already stored" in result.stdout
    with database(tmp_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)


def test_a_rerun_that_cannot_match_says_so_and_is_never_shown(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", "test_app.py::test_clock", expected=0)
    address = "test_app.py::test_clock/app.py:work#20"
    result = ldr(tmp_path, "run", "--detail", address)
    assert result.returncode == 5, result.stdout + result.stderr
    assert "could not reproduce: likely order / time / network dependence" in result.stdout
    assert "--full=test_app.py::test_clock" in result.stdout
    with database(tmp_path) as connection:
        statuses = [json.loads(row[0]) for row in connection.execute("SELECT status FROM runs ORDER BY started_ns")]
    assert [s.get("rerun", {}).get("verified") for s in statuses] == [None, False, False]
    # The default run for `show` is the original, not a rerun that did not match.
    shown = ldr(tmp_path, "show", "--json")
    assert statuses[0]["run_id"] in shown.stdout


def test_the_second_attempt_replays_the_tests_that_ran_before(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", "test_app.py::test_clock", expected=0)
    result = ldr(tmp_path, "run", "--detail", "test_app.py::test_clock/app.py:work#20")
    with database(tmp_path) as connection:
        reruns = [json.loads(row[0])["rerun"] for row in connection.execute(
            "SELECT status FROM runs WHERE json_extract(status, '$.rerun') IS NOT NULL ORDER BY started_ns"
        )]
    assert [item["attempt"] for item in reruns] == [1, 2]
    assert "could not reproduce" in result.stdout


def test_a_test_that_failed_and_then_passed_is_called_flaky(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_flaky", expected=1)
    result = ldr(tmp_path, "run", "--detail", "test_app.py::test_flaky/app.py:work#10")
    assert result.returncode == 5, result.stdout + result.stderr
    assert "failed, then passed on rerun: flaky" in result.stdout
    assert "order / time / network" not in result.stdout


def test_a_different_recorded_environment_value_stops_the_rerun(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", expected=0)
    with database(tmp_path) as connection, connection:
        (record,) = connection.execute("SELECT payload FROM run_environment").fetchone()
        changed = json.loads(record)
        changed["values"]["TZ"] = "Mars/Olympus"
        connection.execute("UPDATE run_environment SET payload = ?", (json.dumps(changed),))
    result = ldr(tmp_path, "run", "--detail", "test_app.py::test_steady/app.py:work#20")
    assert result.returncode == rerun.ENVIRONMENT_DIFFERS, result.stdout + result.stderr
    assert "could not reproduce: the environment differs" in result.stderr
    assert "TZ" in result.stderr


def test_a_script_address_has_no_test_to_rerun(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", expected=0)
    result = ldr(tmp_path, "run", "--detail", "app.py:work#3")
    assert result.returncode == 2 and "pytest test" in result.stdout


def test_the_recorded_random_state_makes_the_rerun_see_the_same_numbers(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", expected=0)
    address = "test_app.py::test_steady/app.py:lucky#1"
    ldr(tmp_path, "run", "--detail", "test_app.py::test_steady/app.py:work#25", expected=0)
    with database(tmp_path) as connection:
        rows = connection.execute(
            "SELECT run_id, signature FROM call_records WHERE frame_id = ? ORDER BY run_id", (address,)
        ).fetchall()
    assert len(rows) == 2 and rows[0][1] == rows[1][1]  # lucky() returned the same value
    assert random.random() < 1


def test_show_prints_the_exact_command_when_detail_was_not_kept(tmp_path):
    project(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py::test_steady", expected=0)
    address = "test_app.py::test_steady/app.py:work#20"
    result = ldr(tmp_path, "show", address)
    assert result.returncode == 2
    assert f"ldr run --detail {address}" in result.stderr
    with database(tmp_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)  # show never reruns
