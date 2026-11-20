"""The child sends its records; the parent writes them and survives the child's death."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def environment(**extra):
    env = dict(os.environ, PYTHONPATH=str(REPO), **extra)
    env.pop("VIRTUAL_ENV", None)
    return env


def stored(project):
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        run_id, status = connection.execute(
            "SELECT id, status FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM call_records WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    return count, json.loads(status) if status else None


def test_only_the_parent_opens_the_database_for_writing(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    log = tmp_path / "connects.log"
    (hooks / "sitecustomize.py").write_text(
        "import os, sys\n"
        "def hook(event, args):\n"
        "    if event == 'sqlite3.connect':\n"
        f"        with open({str(log)!r}, 'a') as handle:\n"
        "            handle.write(f\"{' '.join(sys.orig_argv)}\\t{args[0]}\\n\")\n"
        "sys.addaudithook(hook)\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def f(n):\n    return n\n\nfor n in range(50):\n    f(n)\n", encoding="utf-8")
    env = environment()
    env["PYTHONPATH"] = os.pathsep.join([str(hooks), str(REPO)])
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]
    child = [target for command, target in lines if "ldr.cli" in command and "traces.sqlite3" in str(target)]
    parent = [target for command, target in lines if "ldr.cli" not in command and "traces.sqlite3" in str(target)]
    assert child and all("mode=ro" in str(target) for target in child)
    assert parent and any("mode=rw" in str(target) for target in parent)
    assert stored(project)[0] == 50


def test_a_killed_child_keeps_everything_received_and_the_run_says_where_it_died(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os, signal\n"
        "def f(n):\n    return n\n\n"
        "for n in range(5000):\n    f(n)\n"
        f"os.kill(os.getpid(), {int(KILL)})\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=tmp_path, env=environment(), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    assert "the run died" in result.stderr
    count, status = stored(tmp_path)
    assert 5000 - status["missing_at_most"] <= count <= 5000
    assert status["died"] is True and status["died_at"].startswith("app.py:f#")
    assert json.loads((tmp_path / ".ldr" / "last.json").read_bytes())["status"]["died"] is True


def test_a_hung_child_that_is_killed_loses_at_most_the_last_tenth_of_a_second(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os, time\n"
        "def f(n):\n    return n\n\n"
        "for n in range(3000):\n    f(n)\n"
        "open('ready.txt', 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=tmp_path, env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = tmp_path / "ready.txt"
    deadline = time.monotonic() + 90
    while not (ready.exists() and ready.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), parent.communicate(timeout=5)
    time.sleep(0.5)  # well past the 100 ms flush interval
    os.kill(int(ready.read_text()), KILL)
    _, err = parent.communicate(timeout=60)
    assert parent.returncode != 0 and "the run died" in err
    count, status = stored(tmp_path)
    assert count == 3000
    assert status["died_at"] == "app.py:f#3000"
