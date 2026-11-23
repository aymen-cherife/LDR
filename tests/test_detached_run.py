"""Close-list item 6: one `ldr run` that survives time limits.

Three roles: the waiting command (watches), a detached supervisor (writes the records and
keeps the output and the state), and the program. Killing the waiting command must not stop
the run.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")


def ldr(project, *args, stdin=None, expected=None):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args], cwd=project, env=ENV,
        input=stdin, capture_output=True, text=True, timeout=300,
    )
    if expected is not None:
        assert result.returncode == expected, result.stdout + result.stderr
    return result


def runs(project):
    directory = project / ".ldr" / "runs"
    return sorted(directory.iterdir(), key=lambda path: path.stat().st_mtime) if directory.exists() else []


def state_of(path):
    try:
        return json.loads((path / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_for(condition, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_the_program_exit_code_streams_and_stdin_pass_through(tmp_path):
    (tmp_path / "app.py").write_text(
        "import sys\nprint('to stdout')\nprint('to stderr', file=sys.stderr)\n"
        "print('read:', sys.stdin.read().strip())\nsys.exit(3)\n",
        encoding="utf-8",
    )
    result = ldr(tmp_path, "run", "app.py", stdin="typed text\n")
    assert result.returncode == 3  # the program's own exit code, unchanged
    assert "to stdout" in result.stdout and "read: typed text" in result.stdout
    assert "to stderr" in result.stderr and "to stderr" not in result.stdout
    assert "RUN STATUS: FAILED (exit 3)" in result.stdout  # the result is printed when the run ends


SLOW = """\
import sys, time


def step(n):
    return n + 1


print("started", flush=True)
for i in range(100):
    step(i)
time.sleep(5)
print("finished", flush=True)
"""


def test_killing_the_waiting_command_does_not_stop_the_run(tmp_path):
    (tmp_path / "app.py").write_text(SLOW, encoding="utf-8")
    waiter = subprocess.Popen(
        [sys.executable, "-m", "ldr", "run", "app.py"], cwd=tmp_path, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    directory = wait_for(lambda: next(iter(runs(tmp_path)), None))
    wait_for(lambda: state_of(directory).get("state") == "running")
    wait_for(lambda: "started" in (directory / "stdout.log").read_text(errors="replace")
             if (directory / "stdout.log").exists() else False)
    waiter.kill()  # what a shell's time limit does
    waiter.wait()
    assert state_of(directory).get("state") != "finished"  # killed in the middle of the run

    final = wait_for(lambda: state_of(directory) if state_of(directory).get("state") == "finished" else None)
    assert final["exit_code"] == 0 and final["store_run_id"]
    assert "finished" in (directory / "stdout.log").read_text(errors="replace")
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM call_records WHERE run_id = ?", (final["store_run_id"],)
        ).fetchone() == (100,)  # every call was recorded after the waiter was gone
        status = json.loads(connection.execute("SELECT status FROM runs WHERE id = ?", (final["store_run_id"],)).fetchone()[0])
    assert status["ok"] is True and "died" not in status


def test_status_shows_progress_then_the_result_the_exit_code_and_the_end_of_the_output(tmp_path):
    (tmp_path / "app.py").write_text(SLOW, encoding="utf-8")
    waiter = subprocess.Popen(
        [sys.executable, "-m", "ldr", "run", "app.py"], cwd=tmp_path, env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    directory = wait_for(lambda: next(iter(runs(tmp_path)), None))
    wait_for(lambda: state_of(directory).get("state") == "running" and state_of(directory).get("calls"))
    running = ldr(tmp_path, "status", expected=0).stdout
    assert "running" in running and "call(s) recorded" in running and "slowdown" not in running
    waiter.communicate(timeout=120)
    done = ldr(tmp_path, "status", directory.name, expected=0).stdout
    assert "finished" in done and "exit code 0" in done and "end of output:" in done and "finished" in done.split("end of output:")[1]
    assert "stored run:" in done


def test_a_run_whose_program_is_killed_is_marked_by_the_supervisor(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os\n\n\ndef step(n):\n    return n\n\n\nfor i in range(50):\n    step(i)\nos._exit(9)\n",
        encoding="utf-8",
    )
    result = ldr(tmp_path, "run", "app.py")
    assert result.returncode == 9
    assert "the run died (exit 9)" in result.stderr
    directory = runs(tmp_path)[-1]
    assert state_of(directory)["state"] == "finished" and "died" in state_of(directory)["message"]


def test_progress_is_never_silent_when_the_output_is_not_a_terminal(tmp_path):
    (tmp_path / "app.py").write_text(
        "import time\n\n\ndef step(n):\n    return n\n\n\nfor i in range(20):\n    step(i)\ntime.sleep(13)\n",
        encoding="utf-8",
    )
    result = ldr(tmp_path, "run", "app.py")
    assert result.returncode == 0
    lines = [line for line in result.stderr.splitlines() if line.startswith("[ldr] running")]
    assert lines and "call(s) recorded" in lines[0] and "elapsed" in lines[0] and "slowdown" not in lines[0]


def test_status_without_runs_says_so(tmp_path):
    result = ldr(tmp_path, "status")
    assert result.returncode == 2 and "No runs recorded here." in result.stderr
