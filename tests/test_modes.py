"""Phase 2 rules 12 to 15: default, --deep=, --full= and the mode stored with each run."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

APP = """\
def f(n):
    return n + 1
"""
TESTS = """\
import app


def test_one():
    for index in range(30):
        app.f(index)


def test_two():
    for index in range(30):
        app.f(index)
"""


def ldr(project, *args, expected=None):
    result = processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    if expected is not None:
        assert result.returncode == expected, result.stdout + result.stderr
    return result


def deep_allowed(project):
    (project / "pyproject.toml").write_text(
        "[tool.ldr.storage]\nmax_trace_bytes = 1000000\nmax_deep_bytes = 200000\n", encoding="utf-8"
    )


def made(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "test_app.py").write_text(TESTS, encoding="utf-8")
    return tmp_path


def status(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))["status"]


def kept(project, address):
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM stream_calls WHERE run_id = (SELECT id FROM runs ORDER BY started_ns DESC LIMIT 1)"
            " AND frame_id = ?", (address,),
        ).fetchone()[0]


def test_the_default_keeps_detail_for_only_the_first_calls(tmp_path):
    made(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py", expected=0)
    assert status(tmp_path)["mode"] == "default"
    assert kept(tmp_path, "test_app.py::test_one/app.py:f#3") == 1
    assert kept(tmp_path, "test_app.py::test_one/app.py:f#20") == 0


def test_full_for_one_test_keeps_every_call_of_that_test_only(tmp_path):
    made(tmp_path)
    ldr(tmp_path, "pytest", "test_app.py", "--full=test_app.py::test_one", expected=0)
    assert status(tmp_path)["mode"] == "full=test_app.py::test_one"
    assert kept(tmp_path, "test_app.py::test_one/app.py:f#20") == 1
    assert kept(tmp_path, "test_app.py::test_two/app.py:f#20") == 0


def test_a_bare_full_keeps_every_call_and_does_not_swallow_the_target(tmp_path):
    made(tmp_path)
    ldr(tmp_path, "pytest", "--full", "test_app.py", expected=0)
    assert status(tmp_path)["mode"] == "full=*"
    assert kept(tmp_path, "test_app.py::test_one/app.py:f#20") == 1
    assert kept(tmp_path, "test_app.py::test_two/app.py:f#20") == 1


def test_full_must_name_a_test(tmp_path):
    made(tmp_path)
    result = ldr(tmp_path, "pytest", "test_app.py", "--full=app.py")
    assert result.returncode == 2 and "--full takes a pytest test" in result.stderr


def test_a_bare_deep_means_every_function_and_does_not_swallow_the_target(tmp_path):
    made(tmp_path)
    deep_allowed(tmp_path)
    (tmp_path / "script.py").write_text(APP + "f(1)\nf(2)\n", encoding="utf-8")
    ldr(tmp_path, "run", "--deep", "script.py", expected=0)
    result = status(tmp_path)
    assert result["mode"] == "deep=*"
    assert result["deep_capture"]["functions"] == ["*"] and result["deep_capture"]["matched"] == 2


def test_deep_with_a_name_selects_that_function(tmp_path):
    made(tmp_path)
    deep_allowed(tmp_path)
    (tmp_path / "script.py").write_text(APP + "def g(x):\n    return x\ng(1)\nf(2)\n", encoding="utf-8")
    ldr(tmp_path, "run", "script.py", "--deep=g", expected=0)
    assert status(tmp_path)["deep_capture"]["matched"] == 1


def test_the_old_pruning_switch_is_now_no_prune(tmp_path):
    made(tmp_path)
    ldr(tmp_path, "run", "--no-prune", "app.py", expected=0)
    assert status(tmp_path)["mode"] == "default"


def test_a_run_in_another_mode_compares_fingerprints_and_says_the_modes_differ(tmp_path):
    deep_allowed(tmp_path)
    (tmp_path / "script.py").write_text(APP + "for index in range(20):\n    f(index)\n", encoding="utf-8")
    ldr(tmp_path, "run", "script.py", expected=0)
    result = ldr(tmp_path, "run", "script.py", "--deep=f", expected=0)
    assert status(tmp_path)["reference"]["different"] == 0
    assert "MODE: the previous run was recorded in default mode, this run in deep=f mode" in result.stdout
