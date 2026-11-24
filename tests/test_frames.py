"""Phase 2 rule 1: every call has a frame; only <module> is skipped; generator
expressions and lambdas are calls under their parent and folded in the default view."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

APP = """\
def total(values):
    doubled = [v * 2 for v in values]
    return sum(v + 1 for v in doubled)


def apply(items):
    return list(map(lambda x: x + 1, items))


total([1, 2, 3])
apply([1, 2])
"""


def run(tmp_path, source=APP):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        run_id = connection.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        addresses = [
            row[0]
            for row in connection.execute(
                "SELECT frame_id FROM stream_calls WHERE run_id = ? ORDER BY ordinal", (run_id,)
            )
        ]
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    return addresses, report, (tmp_path / ".ldr" / "last.txt").read_text(encoding="utf-8")


def test_generator_expressions_and_lambdas_are_calls_under_their_parent(tmp_path):
    addresses, _, _ = run(tmp_path)
    assert "app.py:total#1/app.py:total.<locals>.<genexpr>#1" in addresses
    assert "app.py:apply#1/app.py:apply.<locals>.<lambda>#1" in addresses
    assert "app.py:apply#1/app.py:apply.<locals>.<lambda>#2" in addresses


def test_inlined_comprehensions_and_module_frames_are_not_calls(tmp_path):
    addresses, _, _ = run(tmp_path)
    assert not any("<listcomp>" in address or "<module>" in address for address in addresses)


def test_the_default_view_folds_them_into_the_parent(tmp_path):
    _, report, text = run(tmp_path)
    names = [frame["function"] for frame in report["frame_index"]["frames"]]
    assert "total" in names and "apply" in names
    assert not any("<genexpr>" in name or "<lambda>" in name for name in names)
    assert "<genexpr>" not in text and "<lambda>" not in text
