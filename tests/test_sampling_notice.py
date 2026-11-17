"""Until every call is recorded (Phase 2), output must say what sampling hides."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))

LOOP = (
    "def step(i):\n"
    "    return i * 2\n"
    "for i in range(40):\n"
    "    step(i)\n"
)
NOTICE = "calls beyond the first 8 per function are not compared"


def ldr(cwd, *args):
    return subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", "app.py", *args],
        cwd=cwd, env=ENV, capture_output=True, text=True,
    )


def test_sampled_run_says_later_calls_are_not_compared(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)

    result = ldr(tmp_path)

    assert NOTICE in result.stdout


def test_comparison_says_it_too(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)
    ldr(tmp_path, "--golden")

    result = ldr(tmp_path, "--diff")

    assert NOTICE in result.stdout


def test_the_notice_names_the_configured_limit(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)

    result = ldr(tmp_path, "--max-calls-per-function", "3")

    assert "calls beyond the first 3 per function are not compared" in result.stdout


def test_no_notice_when_nothing_was_sampled(tmp_path):
    (tmp_path / "app.py").write_text("def step(i):\n    return i\nstep(1)\n")

    result = ldr(tmp_path)

    assert "not compared" not in result.stdout
