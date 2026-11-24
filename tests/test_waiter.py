"""Hotfix C: the waiting command has no start deadline. It waits while the supervisor's process is
alive; when that process is gone it reads the state once more and either returns the result or says
the supervisor is gone without one. A launcher that fails is reported with the run directory."""

import json
import os
import sys
import threading
import time
from pathlib import Path

from ldr import machine, processes, supervisor, waiter

REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")


def sleeper():
    return processes.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def write_state(directory, **fields):
    (directory / "state.json").write_text(json.dumps(fields), encoding="utf-8")


def stub_launcher(monkeypatch, make):
    """Replace the launcher: ``make(rundir)`` writes what the launcher would, returns (code, stderr)."""
    def spawn(rundir, spec):
        code, stderr = make(Path(rundir))
        return type("Launch", (), {"returncode": code, "stderr": stderr})()
    monkeypatch.setattr(waiter, "spawn_detached", spawn)


def test_a_normal_run_returns_the_programs_exit_code(tmp_path):
    (tmp_path / "app.py").write_text("import sys\n\n\ndef f():\n    return 1\n\n\nf()\nsys.exit(4)\n", encoding="utf-8")
    result = processes.run([sys.executable, "-m", "ldr", "run", "app.py"], cwd=tmp_path, env=ENV, capture_output=True, text=True, timeout=300)
    assert result.returncode == 4
    state = json.loads(next((tmp_path / ".ldr" / "runs").iterdir()).joinpath("state.json").read_text(encoding="utf-8"))
    assert state["state"] == "finished" and state["pid"] and state["pid_start"]


def test_the_launcher_writes_the_supervisors_pid_before_it_exits(tmp_path):
    rundir = tmp_path / ".ldr" / "runs" / "r1"
    rundir.mkdir(parents=True)
    (tmp_path / "app.py").write_text("import time\n\n\ndef f():\n    time.sleep(30)\n\n\nf()\n", encoding="utf-8")
    spec = {"argv": ["run", "app.py"], "project_root": str(tmp_path), "cwd": str(tmp_path), "env": dict(ENV), "started": time.time()}
    launch = supervisor.spawn_detached(str(rundir), spec)
    state = json.loads((rundir / "state.json").read_text(encoding="utf-8"))
    try:
        assert launch.returncode == 0
        assert state["pid"] and machine.process_identity(state["pid"]) is not None  # written, and alive
    finally:
        for pid in (state.get("pid"), json.loads((rundir / "state.json").read_text(encoding="utf-8")).get("program_pid")):
            if pid:
                processes.run(["taskkill", "/PID", str(pid), "/F", "/T"] if os.name == "nt" else ["kill", str(pid)], capture_output=True)


def test_there_is_no_start_deadline(tmp_path, monkeypatch, capsys):
    # The supervisor is alive but slow: a clock that jumps 50 s at every look would have run out
    # the old 120 s limit at once. The waiter keeps waiting and returns the result when it comes.
    monkeypatch.chdir(tmp_path)
    process = sleeper()
    try:
        identity = machine.process_identity(process.pid)

        def make(rundir):
            write_state(rundir, state="starting", pid=process.pid, pid_start=identity[0])
            threading.Timer(1.0, lambda: write_state(rundir, state="finished", exit_code=5, message="the end", pid=process.pid,
                                                    pid_start=identity[0])).start()
            return 0, b""

        stub_launcher(monkeypatch, make)
        clock = {"now": time.time()}

        def jumping():
            clock["now"] += 50
            return clock["now"]

        monkeypatch.setattr(waiter.time, "time", jumping)
        assert waiter.run(["run", "app.py"], str(tmp_path)) == 5
        assert "did not start" not in capsys.readouterr().err
    finally:
        process.kill()
        process.wait()


def test_a_supervisor_that_is_gone_without_a_result_is_reported_with_the_run_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    process = sleeper()
    identity = machine.process_identity(process.pid)

    def make(rundir):
        write_state(rundir, state="running", pid=process.pid, pid_start=identity[0])
        threading.Timer(1.0, lambda: (process.kill(), process.wait())).start()  # no result is ever written
        return 0, b""

    stub_launcher(monkeypatch, make)
    started = time.time()
    assert waiter.run(["run", "app.py"], str(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "the supervisor of run " in err and "is gone without a result" in err and str(tmp_path / ".ldr" / "runs") in err
    assert time.time() - started < 30


def test_a_supervisor_that_exits_right_after_writing_its_result_returns_the_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    process = sleeper()
    identity = machine.process_identity(process.pid)

    def make(rundir):
        write_state(rundir, state="running", pid=process.pid, pid_start=identity[0])

        def end():
            write_state(rundir, state="finished", exit_code=9, pid=process.pid, pid_start=identity[0])
            process.kill()

        threading.Timer(1.0, end).start()
        return 0, b""

    stub_launcher(monkeypatch, make)
    assert waiter.run(["run", "app.py"], str(tmp_path)) == 9


def test_a_pid_given_to_another_process_is_a_supervisor_that_is_gone(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    process = sleeper()  # alive, but it is not the supervisor: it started after the recorded start time

    def make(rundir):
        write_state(rundir, state="running", pid=process.pid, pid_start=time.time() - 3600)
        return 0, b""

    stub_launcher(monkeypatch, make)
    try:
        assert waiter.run(["run", "app.py"], str(tmp_path)) == 2
        assert "is gone without a result" in capsys.readouterr().err
    finally:
        process.kill()
        process.wait()


def test_a_launcher_that_fails_is_reported_with_the_run_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stub_launcher(monkeypatch, lambda rundir: (1, b"Traceback\nImportError: no module named ldr"))
    assert waiter.run(["run", "app.py"], str(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "the launcher of run " in err and "failed (exit 1: ImportError: no module named ldr)" in err
    assert str(tmp_path / ".ldr" / "runs") in err


def test_a_launcher_that_exits_without_a_pid_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stub_launcher(monkeypatch, lambda rundir: (0, b""))
    assert waiter.run(["run", "app.py"], str(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "the launcher of run " in err and "it wrote no process number" in err and str(tmp_path / ".ldr" / "runs") in err


def test_no_fixed_start_limit_is_left_in_the_waiter():
    source = (REPO / "ldr" / "waiter.py").read_text(encoding="utf-8")
    assert "120" not in source and "gone > 100" not in source and "deadline" not in source and "did not start in" not in source


def test_killing_the_waiting_commands_whole_tree_does_not_stop_the_run(tmp_path):
    (tmp_path / "app.py").write_text("import time\n\n\ndef f():\n    time.sleep(8)\n    return 1\n\n\nf()\n", encoding="utf-8")
    waiting = processes.Popen([sys.executable, "-m", "ldr", "run", "app.py"], cwd=tmp_path, env=ENV,
                              stdout=processes.subprocess.DEVNULL, stderr=processes.subprocess.DEVNULL)
    runs = tmp_path / ".ldr" / "runs"
    deadline = time.time() + 120
    state = {}
    while time.time() < deadline:
        found = list(runs.iterdir()) if runs.exists() else []
        if found:
            try:
                state = json.loads((found[0] / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if state.get("program_pid"):
                break
        time.sleep(0.2)
    assert state.get("program_pid")
    command = ["taskkill", "/PID", str(waiting.pid), "/F", "/T"] if os.name == "nt" else ["kill", "-9", str(waiting.pid)]
    processes.run(command, capture_output=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            state = json.loads((found[0] / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("state") == "finished":
            break
        time.sleep(0.5)
    assert state.get("state") == "finished" and state.get("exit_code") == 0
