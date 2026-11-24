"""A run records every call, so it has nothing to say about sampling."""

import os
import subprocess
import sys
from ldr import processes

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
    return processes.run(
        [sys.executable, "-m", "ldr.cli", "run", "app.py", *args],
        cwd=cwd, env=ENV, capture_output=True, text=True,
    )


def test_a_default_run_records_every_call_and_does_not_sample(tmp_path):
    (tmp_path / "app.py").write_text(LOOP)

    result = ldr(tmp_path)

    assert NOTICE not in result.stdout and "sampled" not in result.stdout
    assert "40 project call(s) recorded" in result.stdout

def test_no_notice_when_nothing_was_sampled(tmp_path):
    (tmp_path / "app.py").write_text("def step(i):\n    return i\nstep(1)\n")

    result = ldr(tmp_path)

    assert "not compared" not in result.stdout
