"""``ldr check_machine``: facts, never thresholds. The facts on one line, the processes using the CPU,
then the objective problems and the fixes; what is not LDR's is only named, with the stop commands."""

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


def test_the_first_line_has_every_fact_and_ok_when_there_is_no_problem(ldr_dir):
    report = quick(ldr_dir, sample=True, python_start=True)
    line = report.summary()
    assert line.startswith("machine: ok - 0 run(s) alive, disk ")
    for part in ("GB free (", "runs stored 0 / keep_runs 2", "% busy", "python start ", "Python 3.", "venv ", "s wall, ", "s CPU"):
        assert part in line, (part, line)
    assert "first call" not in line  # alone: a run's first call does not exist
    assert report.cost["wall_s"] >= 0 and report.cost["cpu_s"] >= 0


def test_with_a_run_the_line_has_the_first_call_and_no_python_start(ldr_dir):
    report = machine.check(str(ldr_dir), keep=2, context="run", sample=False)
    assert "first call: none" in report.summary() and "python start" not in report.summary()
    report.finalize(first_call_s=1.25)
    assert "first call 1.25 s" in report.summary()


def test_the_line_counts_problems(ldr_dir):
    write_state(ldr_dir, "dead", state="running", pid=dead_pid(), started_at=time.time())
    report = quick(ldr_dir, fix=False)
    assert report.summary().startswith("machine: 1 problem(s) - ")
    assert report.lines()[1].startswith("- run dead died without a result")


def test_no_threshold_is_left_in_the_check():
    source = (REPO / "ldr" / "machine.py").read_text(encoding="utf-8")
    for name in ("MIN_FREE_BYTES", "MIN_FREE_FRACTION", "BUSY_PERCENT", "HOG_PERCENT", "DATABASE_BIG_BYTES",
                 "PYTHON_START_SECONDS", "FIRST_CALL_SECONDS", "ORPHAN_ROW_HOURS"):
        assert name not in source, name
    harness = (REPO / "evaluation" / "run_phases.py").read_text(encoding="utf-8")
    assert "--wait" not in harness and "wait_for_a_clean_machine" not in harness


def test_a_machine_with_little_free_disk_but_room_for_the_database_has_no_problem(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine.shutil, "disk_usage", lambda path: type("U", (), {"total": 100 * 2**30, "free": 2**20})())
    assert "disk" not in kinds(quick(ldr_dir))  # no database yet: nothing needs the room


def test_less_free_disk_than_the_database_is_a_problem(ldr_dir, monkeypatch):
    database(ldr_dir).close()
    big = ldr_dir / "traces.sqlite3"
    with open(big, "ab") as handle:
        handle.truncate(3 * 2**20)
    monkeypatch.setattr(machine.shutil, "disk_usage", lambda path: type("U", (), {"total": 100 * 2**30, "free": 2 * 2**20})())
    problem = next(p for p in quick(ldr_dir).problems if p.kind == "disk")
    assert "less than the database" in problem.text
    monkeypatch.setattr(machine.shutil, "disk_usage", lambda path: type("U", (), {"total": 100 * 2**30, "free": 4 * 2**20})())
    assert "disk" not in kinds(quick(ldr_dir))


def test_a_run_that_died_a_moment_ago_is_dead_there_is_no_age_rule(ldr_dir):
    directory = write_state(ldr_dir, "justnow", state="running", pid=dead_pid(), started_at=time.time())
    report = quick(ldr_dir)
    assert json.loads((directory / "state.json").read_text(encoding="utf-8"))["state"] == "finished"
    assert any("marked dead run justnow" in fix for fix in report.fixes)
    assert "fixed: marked dead run justnow finished (its supervisor was gone)" in report.lines()


def test_the_rows_of_a_dead_run_are_deleted_and_a_live_runs_rows_are_kept(ldr_dir):
    me = machine.process_identity(os.getpid())
    connection = database(ldr_dir)
    add_run(connection, "dead_row", finished=False)
    add_run(connection, "live_row", finished=False)
    add_run(connection, "orphan", finished=False)
    add_run(connection, "done", finished=True)
    connection.close()
    write_state(ldr_dir, "d", state="running", pid=dead_pid(), started_at=time.time() - 60, store_run_id="dead_row")
    write_state(ldr_dir, "l", state="running", pid=os.getpid(), pid_start=me[0], started_at=time.time(), store_run_id="live_row")
    report = quick(ldr_dir)
    with closing(sqlite3.connect(ldr_dir / "traces.sqlite3")) as connection:
        left = {row[0] for row in connection.execute("SELECT id FROM runs")}
    assert left == {"live_row", "orphan", "done"}  # a live run exists: the orphan may be its own
    assert any("deleted the rows of 1 run(s) that never finished" in fix for fix in report.fixes)


def test_an_unfinished_row_that_no_run_owns_is_deleted_when_nothing_is_alive_or_starting(ldr_dir):
    connection = database(ldr_dir)
    add_run(connection, "orphan", finished=False)
    connection.close()
    (ldr_dir / "runs" / "starting").mkdir(parents=True)  # a run directory with no state yet: it may own the row
    quick(ldr_dir)
    with closing(sqlite3.connect(ldr_dir / "traces.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    (ldr_dir / "runs" / "starting").rmdir()
    report = quick(ldr_dir)
    with closing(sqlite3.connect(ldr_dir / "traces.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert any("deleted the rows of 1 run(s)" in fix for fix in report.fixes)


def test_the_processes_using_the_cpu_are_facts_with_stop_commands_and_ldrs_own_have_none(ldr_dir, monkeypatch):
    stranger = processes.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    supervisor = processes.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # stands for a live run's supervisor
    try:
        identity = machine.process_identity(supervisor.pid)
        write_state(ldr_dir, "l", state="running", pid=supervisor.pid, pid_start=identity[0], started_at=time.time())
        monkeypatch.setattr(machine, "sample_cpu", lambda seconds=1: {"percent": 95.0, "cpus": 8, "processes": {stranger.pid: 90.0, supervisor.pid: 99.0}})
        report = quick(ldr_dir, sample=True)
        assert not [p for p in report.problems if p.kind.startswith("cpu")]  # facts, never problems
        assert "CPU 95% busy" in report.summary()
        lines = report.process_lines()
        assert f"process {supervisor.pid}" in lines[0] and lines[0].endswith("- LDR") and "taskkill" not in lines[0]  # highest first
        assert f"taskkill /PID {stranger.pid} /F" in lines[1] and f"kill {stranger.pid}" in lines[1]
        assert stranger.poll() is None  # not LDR's: never touched
    finally:
        for process in (stranger, supervisor):
            process.kill()
            process.wait()


def test_alone_lists_every_process_and_with_a_run_the_top_five_then_the_count(ldr_dir, monkeypatch):
    many = {pid: float(100 - pid % 90) for pid in range(5000, 5012)}
    monkeypatch.setattr(machine, "sample_cpu", lambda seconds=1: {"percent": 10.0, "cpus": 8, "processes": many})
    alone = quick(ldr_dir, sample=True)
    assert len(alone.process_lines()) == 12 and not any("more" in line for line in alone.process_lines())
    during = machine.check(str(ldr_dir), keep=2, context="run", sample=True)
    lines = during.process_lines()
    assert len(lines) == 6 and lines[-1] == "  and 7 more: ldr check_machine lists them all"


def test_nothing_is_busy_or_slow_however_high_the_numbers(ldr_dir, monkeypatch):
    monkeypatch.setattr(machine, "sample_cpu", lambda seconds=1: {"percent": 100.0, "cpus": 8, "processes": {os.getpid(): 800.0}})
    report = quick(ldr_dir, sample=True)
    assert report.problems == []
    report = machine.Background(str(ldr_dir), 2).finish({"first_call_s": 3600.0}) or report
    assert report.problems == []  # an hour to the first call is a fact, not a problem


def test_the_runs_own_first_call_is_a_fact_in_the_result(ldr_dir):
    background = machine.Background(str(ldr_dir), 2)
    background.thread.join(30)
    report = background.finish({"first_call_s": 42.0})
    assert report.facts["first_call_s"] == 42.0 and "launch of the run to its first recorded call" in report.facts["first_call_measured"]


def test_the_antivirus_facts_are_read_when_possible_and_never_a_problem(ldr_dir):
    report = quick(ldr_dir)
    assert "antivirus" not in kinds(report)
    if os.name == "nt":
        assert set(report.facts["antivirus"]) == {"real_time_scanning", "exclusions_readable", "ldr_excluded"}


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


def test_check_machine_alone_prints_the_summary_first_and_exits_zero(tmp_path):
    (tmp_path / ".ldr").mkdir()
    result = ldr(tmp_path, "check_machine", expected=0)
    first = result.stdout.splitlines()[0]
    assert first.startswith("machine: ") and "python start " in first and "first call" not in first
    data = json.loads((tmp_path / ".ldr" / "machine.json").read_text(encoding="utf-8"))
    assert data["summary"] == first and "cost" in data
    as_json = json.loads(ldr(tmp_path, "check_machine", "--json", expected=0).stdout)
    assert as_json["facts"]["python"]["version"].startswith("3.")


def test_every_run_prints_the_check_keeps_its_exit_code_and_saves_the_result(tmp_path):
    (tmp_path / "app.py").write_text("import sys\n\n\ndef f():\n    return 1\n\n\nf()\nsys.exit(7)\n", encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert result.returncode == 7  # the check never changes the exit code
    assert "[ldr] machine: " in result.stderr
    data = json.loads((tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8"))
    machine_result = data["status"]["machine"]
    assert machine_result["summary"].startswith("machine: ") and machine_result["facts"]["first_call_s"] > 0
    assert "launch of the run to its first recorded call" in machine_result["facts"]["first_call_measured"]
    assert "first call " in machine_result["summary"] and "python start" not in machine_result["summary"]


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
    assert "the check failed" in result.stderr and "check broke" in result.stderr
