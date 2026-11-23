"""The per-run environment record: names only, saved with the run, compared with the previous run."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ldr import environment

REPO = Path(__file__).resolve().parents[1]
SENTINEL_NAME = "LDR_TEST_SENTINEL_NAME"
SENTINEL_VALUE = "value-that-must-never-be-recorded-4471"


def record(**overrides):
    base = {
        "python": {"version": "3.12.4", "implementation": "CPython"},
        "platform": {"system": "linux", "release": "6.8", "tag": "linux-x86_64"},
        "packages": {"pytest": "8.3.1", "requests": "2.31.0"},
        "variables": ["HOME", "PATH"],
    }
    base.update(overrides)
    return base


def test_capture_takes_variable_names_and_never_their_values():
    captured = environment.capture_environment({SENTINEL_NAME: SENTINEL_VALUE, "OTHER": "x"})
    assert captured["variables"] == [SENTINEL_NAME, "OTHER"]
    assert SENTINEL_VALUE not in json.dumps(captured)
    assert captured["python"]["version"].count(".") == 2
    assert "pytest" in captured["packages"]


def test_digest_ignores_key_order_and_follows_content():
    first = record()
    reordered = dict(reversed(list(first.items())))
    assert environment.digest(first) == environment.digest(reordered)
    assert environment.digest(first) != environment.digest(record(variables=["HOME"]))
    summary = environment.summarize(first)
    assert summary["python"] == "3.12.4"
    assert (summary["packages"], summary["variables"]) == (2, 2)
    assert summary["digest"] == environment.digest(first)


def test_compare_lists_each_difference_as_a_fact():
    after = record(
        python={"version": "3.13.1", "implementation": "CPython"},
        packages={"pytest": "9.0.0", "rich": "13.0"},
        variables=["HOME", "NEW_FLAG"],
    )
    lines = [environment.describe_change(c) for c in environment.compare(record(), after)]
    assert lines == [
        "python version 3.12.4 -> 3.13.1",
        "package pytest 8.3.1 -> 9.0.0",
        "package requests removed (was 2.31.0)",
        "package rich added (13.0)",
        "variable name NEW_FLAG added",
        "variable name PATH removed",
    ]
    assert environment.compare(record(), record()) == []


def test_comparison_says_unknown_when_the_previous_run_has_no_record():
    current = environment.summarize(record())
    result = environment.comparison(None, current, None, record())
    assert result["state"] == "unknown"
    assert "recorded no environment" in environment.render_comparison(result, current)


def test_comparison_reports_same_changed_and_unreadable_records():
    current = environment.summarize(record())
    same = environment.comparison(current, current, record(), record())
    assert same["state"] == "same"
    assert "same as the previous run" in environment.render_comparison(same, current)

    newer = record(packages={"pytest": "9.0.0", "requests": "2.31.0"})
    summary = environment.summarize(newer)
    changed = environment.comparison(environment.summarize(record()), summary, record(), newer)
    assert changed["state"] == "changed"
    text = environment.render_comparison(changed, summary)
    assert "ENVIRONMENT CHANGED" in text and "package pytest 8.3.1 -> 9.0.0" in text

    unreadable = environment.comparison(environment.summarize(record()), summary, None, newer)
    assert unreadable["state"] == "changed" and unreadable["changes"] == []
    assert "could not be read" in environment.render_comparison(unreadable, summary)


def test_long_comparisons_list_a_bounded_number_of_lines():
    names = [f"VAR_{index}" for index in range(40)]
    after = record(variables=names)
    summary = environment.summarize(after)
    result = environment.comparison(environment.summarize(record()), summary, record(), after)
    text = environment.render_comparison(result, summary, max_lines=5)
    assert text.count("variable name") == 5
    assert "more; ldr show --environment" in text


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text(
        "def double(x):\n    return x * 2\nprint('RESULT', double(21))\n", encoding="utf-8"
    )
    return tmp_path


def launch(project, *args, extra_env=None, expected_exit=0):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO), **(extra_env or {})),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    return result.stdout


def test_run_saves_names_only_and_show_reads_them_without_running(project):
    launch(project, "run", "app.py", extra_env={SENTINEL_NAME: SENTINEL_VALUE})
    last = project / ".ldr" / "last.json"
    status = json.loads(last.read_bytes())["status"]
    summary = status["environment"]
    assert summary["python"] == environment.capture_environment({})["python"]["version"]
    assert summary["variables"] > 0

    (project / "app.py").unlink()  # reading saved evidence must not need the program
    saved = last.read_bytes()
    shown = json.loads(launch(project, "show", "--environment", "--json"))
    assert shown["run_id"] == status["run_id"]
    assert SENTINEL_NAME in shown["environment"]["variables"]
    assert environment.digest(shown["environment"]) == summary["digest"]
    text = launch(project, "show", "--environment", "--run-id", status["run_id"])
    assert "values are never recorded" in text and SENTINEL_NAME in text
    assert last.read_bytes() == saved
    for path in (last, project / ".ldr" / "last.txt", project / ".ldr" / "traces.sqlite3"):
        assert SENTINEL_VALUE.encode() not in path.read_bytes()
    assert SENTINEL_VALUE not in text


def test_show_environment_names_a_run_that_has_no_record(project):
    launch(project, "run", "app.py")
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        connection.execute("DELETE FROM run_environment")
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "show", "--environment"],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "No environment was recorded for run" in result.stderr


def test_the_comparison_reports_an_environment_change_without_calling_it_a_behaviour_change(project):
    launch(project, "run", "app.py")
    same = launch(project, "run", "app.py", "--fail-on-change")
    assert "ENVIRONMENT: same as the previous run" in same
    last = project / ".ldr" / "last.json"
    assert json.loads(last.read_bytes())["environment_comparison"]["state"] == "same"

    changed = launch(
        project, "run", "app.py", "--fail-on-change",
        extra_env={SENTINEL_NAME: SENTINEL_VALUE},
    )
    assert "ENVIRONMENT CHANGED since the previous run" in changed
    assert f"variable name {SENTINEL_NAME} added" in changed
    assert SENTINEL_VALUE not in changed
    report = json.loads(last.read_bytes())
    assert report["environment_comparison"]["state"] == "changed"
    assert report["behavior"]["changed"] is False


def test_a_previous_run_without_a_record_says_it_did_not_compare(project):
    import sqlite3

    launch(project, "run", "app.py")
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        connection.execute("UPDATE runs SET status = json_remove(status, '$.environment')")
    text = launch(project, "run", "app.py")
    assert "ENVIRONMENT: not compared; the previous run recorded no environment" in text
    report = json.loads((project / ".ldr" / "last.json").read_bytes())
    assert report["environment_comparison"]["state"] == "unknown"
