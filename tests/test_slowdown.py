"""Phase 2 rules 16 and 17, as an internal tool for LDR's developers: the untraced time
(`--untraced-time`), the slowdown and the hot functions. They are kept with the stored run and
never shown to the agent (see test_removed_surface.py)."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from ldr import cli, slowdown
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

APP = """\
def hot(n):
    return n + 1


def once():
    return 1


for index in range(500):
    hot(index)
once()
"""


def ldr(project, *args, expected=0):
    result = processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def stored(project):
    """The status of the latest run, as stored with the run (the developers' copy)."""
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        return json.loads(
            connection.execute("SELECT status FROM runs ORDER BY started_ns DESC LIMIT 1").fetchone()[0]
        )


def shown(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))["status"]


def runs(project):
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        return connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def test_without_an_untraced_time_the_stored_slowdown_says_so_and_nothing_is_run_twice(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert "SLOWDOWN" not in result.stdout
    assert stored(tmp_path)["slowdown"] == {"state": "no baseline"}
    assert "slowdown" not in shown(tmp_path)
    assert runs(tmp_path) == 1


def test_the_untraced_time_is_one_untraced_run_and_later_runs_store_the_factor(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    ldr(tmp_path, "run", "app.py")  # a traced run first: it supplies the file hashes
    result = ldr(tmp_path, "run", "app.py", "--untraced-time")
    assert result.stdout.startswith("untraced time:") and "(exit 0)" in result.stdout
    assert runs(tmp_path) == 1  # the untraced run recorded nothing
    recorded = json.loads((tmp_path / ".ldr" / "baselines.json").read_text(encoding="utf-8"))
    ((key, record),) = recorded.items()
    assert record["seconds"] > 0 and list(record["files"]) == ["app.py"]
    ldr(tmp_path, "run", "app.py")
    info = stored(tmp_path)["slowdown"]
    assert info["state"] == "measured" and info["factor"] > 0 and info["changed_files"] == []
    assert stored(tmp_path)["invocation_key"] == key
    assert "slowdown" not in shown(tmp_path)


def test_a_changed_file_makes_the_untraced_time_stale(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    ldr(tmp_path, "run", "app.py", "--untraced-time")
    (tmp_path / "app.py").write_text(APP + "# edited\n", encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    info = stored(tmp_path)["slowdown"]
    assert info["state"] == "stale" and info["changed_files"] == ["app.py"]


def test_an_untraced_time_without_file_hashes_says_staleness_cannot_be_checked(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py", "--untraced-time")
    assert "no file hashes" in result.stdout
    ldr(tmp_path, "run", "app.py")
    assert stored(tmp_path)["slowdown"]["file_hashes_recorded"] is False


def test_above_five_times_it_suggests_a_narrower_trace():
    info = {
        "state": "measured", "factor": 7.25, "traced_seconds": 7.25, "untraced_seconds": 1.0,
        "changed_files": [], "file_hashes_recorded": True,
        "highest_cost": [{"function": "app.py:f", "nodes": 12345}],
    }
    text = slowdown.render_slowdown(info)
    assert "7.2x" in text or "7.3x" in text
    assert "highest fingerprint cost: app.py:f (12,345 nodes)" in text and "--only <directory>" in text
    assert "--only" not in slowdown.render_slowdown({**info, "factor": 3.0})


def test_the_slowdown_arithmetic():
    baseline = {"seconds": 2.0, "files": {"a.py": "1"}}
    measured = slowdown.slowdown(5.0, baseline, {"a.py": "1"}, [])
    assert (measured["state"], measured["factor"]) == ("measured", 2.5)
    assert slowdown.slowdown(5.0, baseline, {"a.py": "2"}, [])["changed_files"] == ["a.py"]
    assert slowdown.slowdown(5.0, baseline, {"a.py": "1", "b.py": "9"}, [])["state"] == "stale"
    assert slowdown.slowdown(5.0, None, {}, []) == {"state": "no baseline"}


def test_hot_functions_are_recorded_for_developers_and_not_shown(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    hot = stored(tmp_path)["hot_functions"]
    assert hot[0]["function"] == "app.py:hot" and hot[0]["calls"] == 500
    assert hot[0]["share"] > 0.99 and hot[0]["fingerprint_nodes"] > 0
    assert "HOT FUNCTIONS" not in result.stdout and "hot_functions" not in shown(tmp_path)


def test_a_small_run_lists_no_hot_functions(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\nf()\n", encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    assert stored(tmp_path)["hot_functions"] == []


def test_printed_output_is_limited_to_30000_characters():
    text = cli._limit_text("x" * 79 + "\n" * 1 + ("y" * 79 + "\n") * 3000, max_lines=10**9)
    assert len(text) <= 30_000
