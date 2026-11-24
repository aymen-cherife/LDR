"""Hotfix step 1: ``ldr check_machine``. One summary line, then one line per problem; LDR's own
leftovers are fixed and said so; what is not LDR's is only named, with the number and the commands."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from ldr import machine
from ldr.sqlite_store import SCHEMA
from ldr import processes

REPO = Path(__file__).resolve().parents[1]


def ldr(project, *args, expected=None):
    result = processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    if expected is not None:
        assert result.returncode == expected, result.stdout + result.stderr
    return result


def dead_pid():
    process = processes.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def write_state(ldr_dir, name, **fields):
    directory = Path(ldr_dir) / "runs" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.json").write_text(json.dumps(fields), encoding="utf-8")
    return directory


def database(ldr_dir):
    path = Path(ldr_dir) / "traces.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def add_run(connection, run_id, *, finished, age_hours=0.0, command="a.py"):
    invocation = json.dumps({"runner": "run", "target": command, "arguments": []})
    started = int((time.time() - age_hours * 3600) * 1e9)
    with connection:
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?)", (run_id, started, invocation, json.dumps({}) if finished else None)
        )
        connection.execute("INSERT INTO stream_budget (run_id) VALUES (?)", (run_id,))


def quick(ldr_dir, **options):
    options.setdefault("python_start", False)  # a real start takes seconds on a slow machine
    return machine.check(str(ldr_dir), keep=options.pop("keep", 2), sample=options.pop("sample", False), **options)


@pytest.fixture
def ldr_dir(tmp_path):
    path = tmp_path / ".ldr"
    path.mkdir()
    return path


def kinds(report):
    return [problem.kind for problem in report.problems]


def test_the_summary_comes_first_then_one_line_per_problem(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine.shutil, "disk_usage", lambda path: type("U", (), {"total": 100 * 2**30, "free": 2**30})())
    report = quick(ldr_dir)
    lines = report.lines()
    assert lines[0].startswith("machine: 1 problem(s)") and lines[1].startswith("- only 1.0 GB free")
    assert len(lines) == 2


def test_a_machine_without_problems_says_ok_and_how_much_it_cost(ldr_dir):
    report = quick(ldr_dir)
    assert report.summary.startswith("machine: ok - ") and "check " in report.summary
    assert report.cost["wall_s"] >= 0 and report.cost["cpu_s"] >= 0
    assert len(report.lines()) == 1


def test_low_disk_is_a_problem_with_the_commands(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine.shutil, "disk_usage", lambda path: type("U", (), {"total": 100 * 2**30, "free": 5 * 2**30})())
    assert "disk" not in kinds(quick(ldr_dir))  # 5 GB of 100 is enough
    monkeypatch.setattr(machine.shutil, "disk_usage", lambda path: type("U", (), {"total": 100 * 2**30, "free": 2 * 2**30 - 1})())
    problem = next(p for p in quick(ldr_dir).problems if p.kind == "disk")
    assert problem.windows and problem.linux


def test_a_dead_run_is_marked_finished_and_said(ldr_dir):
    me = machine.process_identity(os.getpid())
    gone = dead_pid()
    directory = write_state(ldr_dir, "dead1", state="running", pid=gone, pid_start=me[0], started_at=time.time() - 60)
    report = quick(ldr_dir)
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "finished" and state["exit_code"] == 2 and "marked this run dead" in state["message"]
    assert any("marked dead run dead1 finished" in fix for fix in report.fixes)
    assert "dead_run" not in kinds(report)


def test_a_dead_run_stays_a_problem_when_fixing_is_off(ldr_dir):
    directory = write_state(ldr_dir, "dead2", state="running", pid=dead_pid(), started_at=time.time() - 60)
    report = quick(ldr_dir, fix=False)
    assert "dead_run" in kinds(report) and not report.fixes
    assert json.loads((directory / "state.json").read_text(encoding="utf-8"))["state"] == "running"


def test_a_live_run_is_never_touched(ldr_dir):
    me = machine.process_identity(os.getpid())
    directory = write_state(ldr_dir, "live1", state="running", pid=os.getpid(), pid_start=me[0], started_at=time.time())
    before = (directory / "state.json").read_text(encoding="utf-8")
    report = quick(ldr_dir)
    assert (directory / "state.json").read_text(encoding="utf-8") == before
    assert report.facts["runs_alive"] == 1 and not report.fixes


def test_a_reused_process_id_is_not_trusted(ldr_dir):
    # The number is alive, but it is another process that started long after the run did.
    me = machine.process_identity(os.getpid())
    directory = write_state(
        ldr_dir, "reused", state="running", pid=os.getpid(), pid_start=me[0] - 3600, started_at=time.time() - 3600
    )
    report = quick(ldr_dir)
    assert json.loads((directory / "state.json").read_text(encoding="utf-8"))["state"] == "finished"
    assert report.facts["runs_dead"] == 1


def test_a_reused_process_id_of_another_program_is_not_trusted(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine, "process_identity", lambda pid: (time.time(), "notepad.exe") if pid else None)
    state = {"pid": 4242, "pid_start": time.time(), "started_at": time.time()}
    assert machine.supervisor_alive(state) is False  # right start time, wrong program
    monkeypatch.setattr(machine, "process_identity", lambda pid: (time.time(), "python.exe"))
    assert machine.supervisor_alive(state) is True


def test_a_state_without_a_start_time_is_judged_by_the_run_start(ldr_dir, monkeypatch):
    now = time.time()
    monkeypatch.setattr(machine, "process_identity", lambda pid: (now - 100, "python.exe"))
    assert machine.supervisor_alive({"pid": 1, "started_at": now}) is False  # the process is older than the run
    assert machine.supervisor_alive({"pid": 1, "started_at": now - 200}) is True


def test_the_process_identity_of_this_process_and_of_a_missing_one():
    start, name = machine.process_identity(os.getpid())
    assert "python" in name and abs(time.time() - start) < 86400 * 365
    assert machine.process_identity(dead_pid()) is None
    assert machine.process_identity(0) is None


def test_the_rows_of_a_dead_run_are_deleted_and_a_live_run_keeps_its_rows(ldr_dir):
    me = machine.process_identity(os.getpid())
    connection = database(ldr_dir)
    add_run(connection, "dead_row", finished=False)
    add_run(connection, "live_row", finished=False)
    add_run(connection, "recent_orphan", finished=False, age_hours=1)
    add_run(connection, "old_orphan", finished=False, age_hours=10)
    add_run(connection, "done", finished=True)
    connection.close()
    write_state(ldr_dir, "d", state="running", pid=dead_pid(), started_at=time.time() - 60, store_run_id="dead_row")
    write_state(ldr_dir, "l", state="running", pid=os.getpid(), pid_start=me[0], started_at=time.time(), store_run_id="live_row")
    report = quick(ldr_dir)
    with closing(sqlite3.connect(ldr_dir / "traces.sqlite3")) as connection:
        left = {row[0] for row in connection.execute("SELECT id FROM runs")}
        assert not connection.execute("SELECT 1 FROM stream_budget WHERE run_id IN ('dead_row', 'old_orphan')").fetchone()
    assert left == {"live_row", "recent_orphan", "done"}
    assert any("deleted the rows of 2 run(s) that never finished" in fix for fix in report.fixes)


def test_runs_beyond_keep_runs_are_deleted_when_alone_and_only_named_during_a_run(ldr_dir):
    connection = database(ldr_dir)
    for index in range(4):
        add_run(connection, f"r{index}", finished=True, age_hours=10 - index)
    connection.close()
    report = quick(ldr_dir, context="run")
    assert "keep_runs" in kinds(report) and "deleted at the end of this run" in report.problems[0].text and not report.fixes
    report = quick(ldr_dir, keep=2)
    with closing(sqlite3.connect(ldr_dir / "traces.sqlite3")) as connection:
        assert {row[0] for row in connection.execute("SELECT id FROM runs")} == {"r2", "r3"}
    assert any("deleted 2 stored run(s) beyond keep_runs = 2" in fix for fix in report.fixes)
    assert "keep_runs" not in kinds(report)


def test_a_locked_database_names_the_process_that_holds_it(ldr_dir):
    database(ldr_dir).close()
    path = str(ldr_dir / "traces.sqlite3")
    holder = processes.Popen(
        [sys.executable, "-c",
         "import sqlite3,sys,time\n"
         "c=sqlite3.connect(sys.argv[1]); c.isolation_level=None; c.execute('BEGIN EXCLUSIVE')\n"
         "print('held', flush=True); time.sleep(60)", path],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        report = quick(ldr_dir)
        problem = next(p for p in report.problems if p.kind == "database_locked")
        # the virtual environment's python.exe starts the real interpreter: the holder is it or its child
        family = {holder.pid} | {pid for pid, parent, _ in machine.list_processes() if parent == holder.pid}
        assert problem.number in family, report.lines()
        assert str(problem.number) in problem.windows and str(problem.number) in problem.linux
        assert report.facts["database_locked"] is True
    finally:
        holder.kill()
        holder.wait()
    assert "database_locked" not in kinds(quick(ldr_dir))


def test_a_busy_process_that_is_not_ldrs_is_named_with_its_number_and_never_stopped(ldr_dir, monkeypatch):
    stranger = processes.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(machine, "sample_cpu", lambda seconds=1: {"percent": 70.0, "cpus": 8, "processes": {stranger.pid: 95.0, os.getpid(): 90.0}})
        report = quick(ldr_dir, sample=True)
        problems = [p for p in report.problems if p.kind == "cpu_process"]
        assert [p.number for p in problems] == [stranger.pid]  # this process is not named
        assert f"taskkill /PID {stranger.pid} /F" == problems[0].windows and f"kill {stranger.pid}" == problems[0].linux
        assert stranger.poll() is None  # still running: it is not LDR's
    finally:
        stranger.kill()
        stranger.wait()


def test_the_processes_of_a_live_ldr_run_are_not_blamed(ldr_dir, monkeypatch):
    me = machine.process_identity(os.getpid())
    write_state(ldr_dir, "l", state="running", pid=os.getpid(), pid_start=me[0], program_pid=os.getpid(), started_at=time.time())
    monkeypatch.setattr(machine, "sample_cpu", lambda seconds=1: {"percent": 40.0, "cpus": 8, "processes": {os.getpid(): 99.0}})
    assert not [p for p in quick(ldr_dir, sample=True).problems if p.kind.startswith("cpu")]


def test_a_busy_cpu_with_no_single_culprit_is_a_problem(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine, "sample_cpu", lambda seconds=1: {"percent": 85.0, "cpus": 8, "processes": {}})
    assert "cpu" in kinds(quick(ldr_dir, sample=True))


def test_slow_python_start_is_a_problem(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine, "PYTHON_START_SECONDS", 0.0)
    assert "python_start" in kinds(quick(ldr_dir, python_start=True))


def test_the_runs_own_python_start_time_is_judged_from_the_state(ldr_dir, tmp_path):
    background = machine.Background(str(ldr_dir), 2)
    background.thread.join(30)
    report = background.finish({"first_call_s": 42.0})
    assert report.facts["python_start_s"] == 42.0 and "python_start" in kinds(report)
    assert "launch of the run to its first recorded call" in report.facts["python_start_measured"]


def test_a_broken_python_environment_is_a_problem(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine.sys, "prefix", str(ldr_dir))
    monkeypatch.setattr(machine, "_venv_home", lambda: str(ldr_dir / "gone"))
    assert "python" in kinds(quick(ldr_dir))


@pytest.mark.skipif(os.name != "nt", reason="the antivirus check is Windows only")
def test_antivirus_scanning_that_covers_ldr_is_a_problem_only_when_readable(ldr_dir, monkeypatch):
    import types

    values = {"DisableRealtimeMonitoring": 0}
    paths = {"value": None}

    class Key:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, subkey):
        if subkey.endswith("Exclusions\\Paths") and paths["value"] is None:
            raise PermissionError("denied")
        return Key(subkey)

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=0, OpenKey=open_key, QueryValueEx=lambda key, name: (values[name], 4),
        QueryInfoKey=lambda key: (0, len(paths["value"] or []), 0), EnumValue=lambda key, i: (paths["value"][i], "", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert "antivirus" not in kinds(quick(ldr_dir))  # exclusions not readable without admin: no claim
    paths["value"] = [r"C:\elsewhere"]
    problem = next(p for p in quick(ldr_dir).problems if p.kind == "antivirus")
    assert "Add-MpPreference" in problem.windows
    paths["value"] = [str(ldr_dir.parent)]
    assert "antivirus" not in kinds(quick(ldr_dir))
    paths["value"] = [r"C:\elsewhere"]
    values["DisableRealtimeMonitoring"] = 1
    assert "antivirus" not in kinds(quick(ldr_dir))


def test_check_machine_alone_prints_the_summary_first_and_exits_zero(tmp_path):
    (tmp_path / ".ldr").mkdir()
    result = ldr(tmp_path, "check_machine", expected=0)
    assert result.stdout.splitlines()[0].startswith("machine: ")
    data = json.loads((tmp_path / ".ldr" / "machine.json").read_text(encoding="utf-8"))
    assert data["summary"] == result.stdout.splitlines()[0] and "cost" in data
    as_json = json.loads(ldr(tmp_path, "check_machine", "--json", expected=0).stdout)
    assert as_json["facts"]["python"]["version"].startswith("3.")


def test_every_run_prints_the_check_keeps_its_exit_code_and_saves_the_result(tmp_path):
    (tmp_path / "app.py").write_text("import sys\n\n\ndef f():\n    return 1\n\n\nf()\nsys.exit(7)\n", encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert result.returncode == 7  # the check never changes the exit code
    assert "[ldr] machine: " in result.stderr
    data = json.loads((tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8"))
    machine_result = data["status"]["machine"]
    assert machine_result["summary"].startswith("machine: ") and machine_result["facts"]["python_start_s"] > 0
    assert "launch of the run to its first recorded call" in machine_result["facts"]["python_start_measured"]


def test_a_check_that_fails_does_not_fail_the_run(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n\n\nf()\n", encoding="utf-8")
    (tmp_path / "sitecustomize.py").write_text(
        "import ldr.machine as m\n\n\ndef boom(*a, **k):\n    raise RuntimeError('check broke')\n\n\nm.check = boom\n", encoding="utf-8"
    )
    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "app.py"], cwd=tmp_path, capture_output=True, text=True, timeout=300,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO), str(tmp_path)])),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "machine: the check failed" in result.stderr
