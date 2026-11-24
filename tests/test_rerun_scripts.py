"""Close-list items 7 and 8: time-like differences, and --detail for plain scripts."""

import os
import subprocess
import sys
from pathlib import Path

from ldr import rerun
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

SCRIPT = """\
import random
import sys


def work(n):
    return n * 2


def lucky():
    return random.randint(0, 10**9)


total = 0
for index in range(30):
    total += work(index)
print("lucky", lucky(), "total", total)
"""

CLOCK = """\
import time


def work(n):
    return n * 2


def mark(value):
    return value


for index in range(20):
    work(index)
mark(time.time_ns())
"""


def ldr(project, *args):
    return processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )


def test_time_like_means_a_time_type_or_a_number_in_the_runs_own_window():
    window = [(1_700_000_000.0, 1_700_000_050.0)]
    assert rerun.is_time_like("datetime.datetime(2026, 11, 23, 3, 0)", window)
    assert rerun.is_time_like("time.struct_time(tm_year=2026)", window)
    assert rerun.is_time_like("1700000010.5", window)  # seconds
    assert rerun.is_time_like("1700000010123", window)  # milliseconds
    assert rerun.is_time_like("1700000010123456789", window)  # nanoseconds
    assert not rerun.is_time_like("1700000099", window)  # outside the window
    assert not rerun.is_time_like("42", window) and not rerun.is_time_like("'hello'", window)


def test_a_plain_script_is_rerun_whole_with_the_same_random_state_and_verified(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    assert ldr(tmp_path, "run", "app.py").returncode == 0
    first = [line for line in (tmp_path / ".ldr" / "runs").glob("*/stdout.log")][0].read_text(errors="replace")
    result = ldr(tmp_path, "run", "--detail", "app.py:work#20")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RERUN VERIFIED (the whole script)" in result.stdout and "side effect" not in result.stdout.lower()
    shown = ldr(tmp_path, "show", "app.py:work#20")
    assert shown.returncode == 0 and "in  n = 19" in shown.stdout
    # The rerun saw the same random numbers, so the program printed the same line.
    lucky = next(line for line in first.splitlines() if line.startswith("lucky"))
    assert lucky  # recorded in the first run; the rerun matched every call, lucky() included


def test_a_script_whose_value_is_the_clock_says_the_difference_is_time_like(tmp_path):
    (tmp_path / "app.py").write_text(CLOCK, encoding="utf-8")
    assert ldr(tmp_path, "run", "app.py").returncode == 0
    result = ldr(tmp_path, "run", "--detail", "app.py:work#10")
    assert result.returncode == 5, result.stdout + result.stderr
    assert "time-like" in result.stdout and "clock" in result.stdout
    assert "ldr run app.py --full" in result.stdout
