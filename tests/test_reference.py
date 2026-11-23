"""Phase 2 rule 4: calls are compared with the previous run of the same target.

A finished call asks SQLite for the signature of the call at the same address in the previous
run; a call that differs keeps full detail, with no rerun. After the run the whole comparison
is a join over the two runs' rows (see previous.py and test_previous_run.py)."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TEMPLATE = """\
{padding}def f(n):
    return n * 2 + ({delta} if n == 12 else 0)


for index in range({count}):
    f(index + 1)
"""


def ldr(project, *args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def write(project, *, delta=0, count=30, padding=""):
    (project / "app.py").write_text(
        TEMPLATE.format(delta=delta, count=count, padding=padding), encoding="utf-8"
    )


def report(project):
    return json.loads((project / ".ldr" / "last.json").read_bytes())


def database(project):
    return sqlite3.connect(project / ".ldr" / "traces.sqlite3")


def test_a_differing_call_beyond_the_first_eight_keeps_detail_without_a_rerun(tmp_path):
    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    write(tmp_path, delta=1)
    ldr(tmp_path, "run", "app.py")
    result = report(tmp_path)
    reference = result["status"]["reference"]
    assert (reference["compared"], reference["same"], reference["different"]) == (30, 29, 1)
    assert result["behavior"]["stream_changed_functions"] == ["app.py:f"]
    run_id = result["status"]["run_id"]
    with database(tmp_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (2,)  # no rerun
        kept = {
            row[0]
            for row in connection.execute(
                "SELECT frame_id FROM stream_calls WHERE run_id = ? AND frame_id LIKE 'app.py:f#%'",
                (run_id,),
            )
        }
    assert kept == {f"app.py:f#{n}" for n in range(1, 9)} | {"app.py:f#12"}
    payload = json.loads(
        database(tmp_path).execute(
            "SELECT payload FROM stream_calls WHERE run_id = ? AND frame_id = 'app.py:f#12'", (run_id,)
        ).fetchone()[0]
    )
    assert payload["detail_at"] == "return"
    assert payload["input_preview"] == {"n": "12"}
    assert "differs from reference" in payload["kept"]


def test_moving_the_function_does_not_make_its_calls_differ(tmp_path):
    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    write(tmp_path, padding="# moved down\n# by two lines\n")
    ldr(tmp_path, "run", "app.py")
    reference = report(tmp_path)["status"]["reference"]
    assert (reference["compared"], reference["different"], reference["new"]) == (30, 0, 0)


def test_new_and_missing_calls_are_reported(tmp_path):
    write(tmp_path, count=30)
    ldr(tmp_path, "run", "app.py")
    write(tmp_path, count=28)
    ldr(tmp_path, "run", "app.py")
    reference = report(tmp_path)["status"]["reference"]
    assert (reference["compared"], reference["same"], reference["missing"]) == (28, 28, 2)
    assert report(tmp_path)["behavior"]["stream_changed_functions"] == ["app.py:f"]
    write(tmp_path, count=31)
    ldr(tmp_path, "run", "app.py")
    reference = report(tmp_path)["status"]["reference"]
    assert (reference["new"], reference["missing"]) == (3, 0)  # against the run of 28, not of 30


def test_the_previous_run_is_stated_in_the_output(tmp_path):
    write(tmp_path)
    ldr(tmp_path, "run", "app.py")
    write(tmp_path, delta=1)
    result = ldr(tmp_path, "run", "app.py")
    assert "PREVIOUS RUN" in result.stdout and "1 differ" in result.stdout
