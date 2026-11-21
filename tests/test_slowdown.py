"""Phase 2 rules 16 and 17: the untraced baseline, the slowdown, and the hot functions."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from ldr import cli, slowdown

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
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def status(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))["status"]


def runs(project):
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        return connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def test_without_a_baseline_the_slowdown_says_so_and_nothing_is_run_twice(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert "SLOWDOWN: no baseline" in result.stdout
    assert status(tmp_path)["slowdown"] == {"state": "no baseline"}
    assert runs(tmp_path) == 1


def test_a_baseline_is_one_untraced_run_and_later_runs_state_the_factor(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    ldr(tmp_path, "run", "app.py")  # a traced run first: it supplies the file hashes
    result = ldr(tmp_path, "run", "app.py", "--baseline")
    assert result.stdout.startswith("BASELINE:") and "untraced (exit 0)" in result.stdout
    assert runs(tmp_path) == 1  # the baseline recorded nothing
    recorded = json.loads((tmp_path / ".ldr" / "baselines.json").read_text(encoding="utf-8"))
    ((key, record),) = recorded.items()
    assert record["seconds"] > 0 and list(record["files"]) == ["app.py"]
    traced = ldr(tmp_path, "run", "app.py")
    info = status(tmp_path)["slowdown"]
    assert info["state"] == "measured" and info["factor"] > 0 and info["changed_files"] == []
    assert f"SLOWDOWN: {info['factor']:.1f}x" in traced.stdout
    assert status(tmp_path)["invocation_key"] == key


def test_a_changed_file_makes_the_baseline_stale(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    ldr(tmp_path, "run", "app.py", "--baseline")
    (tmp_path / "app.py").write_text(APP + "# edited\n", encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert status(tmp_path)["slowdown"]["state"] == "stale"
    assert "baseline stale (changed since it was recorded: app.py)" in result.stdout


def test_a_baseline_without_file_hashes_says_staleness_cannot_be_checked(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py", "--baseline")
    assert "no file hashes" in result.stdout
    traced = ldr(tmp_path, "run", "app.py")
    assert "baseline stale cannot be checked" in traced.stdout


def test_baseline_cannot_be_combined_with_recording_options(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    assert ldr(tmp_path, "run", "app.py", "--baseline", "--golden", expected=2).returncode == 2


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


def test_hot_functions_are_reported_not_skipped(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    hot = status(tmp_path)["hot_functions"]
    assert hot[0]["function"] == "app.py:hot" and hot[0]["calls"] == 500
    assert hot[0]["share"] > 0.99 and hot[0]["fingerprint_nodes"] > 0
    assert "HOT FUNCTIONS (of 501 calls): app.py:hot 500 calls" in result.stdout
    assert "They are recorded, not skipped." in result.stdout


def test_a_small_run_lists_no_hot_functions(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\nf()\n", encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert status(tmp_path)["hot_functions"] == [] and "HOT FUNCTIONS" not in result.stdout


def test_printed_output_is_limited_to_30000_characters():
    text = cli._limit_text("x" * 79 + "\n" * 1 + ("y" * 79 + "\n") * 3000, max_lines=10**9)
    assert len(text) <= 30_000
