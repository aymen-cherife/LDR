"""A default run records every call, so it has nothing to say about sampling.

Snapshots (--golden, --diff) still use the capped tree and still say what it samples.
"""

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


def test_a_default_run_records_every_call_and_does_not_sample(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)

    result = ldr(tmp_path)

    assert NOTICE not in result.stdout and "sampled" not in result.stdout
    assert "40 project call(s) recorded" in result.stdout

def test_a_snapshot_run_says_what_the_tree_samples(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)

    result = ldr(tmp_path, "--golden")

    assert NOTICE in result.stdout


def test_comparison_says_it_too(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)
    ldr(tmp_path, "--golden")

    result = ldr(tmp_path, "--diff")

    assert NOTICE in result.stdout


def test_the_notice_names_the_configured_limit(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)

    result = ldr(tmp_path, "--golden", "--max-calls-per-function", "3")

    assert "calls beyond the first 3 per function are not compared" in result.stdout


def test_no_notice_when_nothing_was_sampled(tmp_path):
    (tmp_path / "app.py").write_text("def step(i):\n    return i\nstep(1)\n")

    result = ldr(tmp_path)

    assert "not compared" not in result.stdout
