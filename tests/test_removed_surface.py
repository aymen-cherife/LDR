"""Batch 1, step 3: no golden run, no snapshots, no verify, no volatility learning. The untraced
timing is `--untraced-time` and is hidden from the agent."""

import os
import subprocess
import sys
from pathlib import Path

import importlib
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

SCRIPT = "import sys\n\n\ndef work(n):\n    return n * 2\n\n\nprint('argv', sys.argv[1:], work(21))\n"


def ldr(project, *args):
    return processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )


def test_verify_is_not_a_command(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    result = ldr(tmp_path, "verify", "app.py")
    assert result.returncode == 2 and "invalid choice" in result.stderr


def test_the_removed_flags_are_the_programs_own_arguments(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py", "--golden", "--diff", "--learn-volatility", "--snapshot-name=x")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "argv ['--golden', '--diff', '--learn-volatility', '--snapshot-name=x'] 42" in result.stdout
    assert not (tmp_path / ".ldr" / "snapshots").exists()


def test_no_snapshot_code_is_left_in_the_store():
    for name in ("snapshot_path", "save_trace", "load_trace"):
        assert not hasattr(importlib.import_module("ldr.store"), name)


def test_help_says_nothing_about_removed_or_internal_options(tmp_path):
    text = ldr(tmp_path, "--help").stdout.lower()
    for word in ("golden", "snapshot", "learn-volatility", "--diff", "verify", "untraced", "baseline", "slowdown"):
        assert word not in text, word


def test_the_untraced_time_is_recorded_and_never_shown_to_the_agent(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    timed = ldr(tmp_path, "run", "app.py", "--untraced-time")
    assert timed.returncode == 0, timed.stdout + timed.stderr
    result = ldr(tmp_path, "run", "app.py")
    assert result.returncode == 0, result.stdout + result.stderr
    shown = result.stdout + result.stderr + (tmp_path / ".ldr" / "last.txt").read_text(encoding="utf-8")
    shown += (tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8")
    for word in ("slowdown", "baseline", "untraced", "HOT FUNCTIONS"):
        assert word.lower() not in shown.lower(), word


def test_the_old_name_is_not_a_flag_any_more(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py", "--baseline")
    assert "argv ['--baseline'] 42" in result.stdout
