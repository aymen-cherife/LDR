"""Speed step 2: no running digest per function; per-call records are the only comparison."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from ldr import call_stream
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

APP = """\
def f(n):
    return n * 2


for index in range(20):
    f(index)
"""


def test_the_stream_summary_has_counts_but_no_per_function_digest(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        (text,) = connection.execute("SELECT summary FROM stream_summary").fetchone()
        records = connection.execute("SELECT COUNT(*) FROM call_records").fetchone()[0]
    summary = json.loads(text)
    assert "functions" not in summary
    assert (summary["started"], summary["fingerprinted"], summary["uncomparable"]) == (20, 20, 0)
    assert records == 20  # every call is still recorded


def test_the_digest_comparison_is_gone():
    assert not hasattr(call_stream, "changed_functions")
