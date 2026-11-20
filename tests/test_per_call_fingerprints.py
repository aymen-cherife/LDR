"""Phase 2 rule 2: every call keeps per-call fingerprints; detail only for the first N."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

APP = """\
def make(n):
    return list(range(n))


def use(items):
    return len(items)


def f(value):
    return value * 2


data = make(3)
use(data)
for index in range(20):
    f(index)
"""


def run(tmp_path, settings="", source=APP):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    if settings:
        (tmp_path / "pyproject.toml").write_text("[tool.ldr.storage]\n" + settings, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    connection = sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")
    run_id = connection.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    return connection, run_id


def test_every_call_has_fingerprint_rows_and_only_the_first_eight_have_detail(tmp_path):
    connection, run_id = run(tmp_path)
    fingerprinted = connection.execute(
        "SELECT COUNT(*) FROM stream_fp WHERE run_id = ? AND frame_id LIKE 'app.py:f#%'", (run_id,)
    ).fetchone()[0]
    detailed = connection.execute(
        "SELECT COUNT(*) FROM stream_calls WHERE run_id = ? AND frame_id LIKE 'app.py:f#%'", (run_id,)
    ).fetchone()[0]
    assert (fingerprinted, detailed) == (20, 8)


def test_the_twelfth_call_keeps_its_argument_and_return_fingerprints(tmp_path):
    connection, run_id = run(tmp_path)
    rows = connection.execute(
        "SELECT v.role, v.name FROM stream_fp_values v JOIN stream_fp c "
        "ON c.run_id = v.run_id AND c.worker = v.worker AND c.ordinal = v.ordinal "
        "WHERE c.run_id = ? AND c.frame_id = 'app.py:f#12' ORDER BY v.role",
        (run_id,),
    ).fetchall()
    assert rows == [("in", "value"), ("ret", "")]


def test_an_argument_can_be_matched_to_another_calls_return_by_fingerprint(tmp_path):
    connection, run_id = run(tmp_path)
    matches = connection.execute(
        "SELECT c1.frame_id, a.name, c2.frame_id FROM stream_fp_values a "
        "JOIN stream_fp_values r ON r.run_id = a.run_id AND r.fp = a.fp AND r.role = 'ret' "
        "JOIN stream_fp c1 ON c1.run_id = a.run_id AND c1.worker = a.worker AND c1.ordinal = a.ordinal "
        "JOIN stream_fp c2 ON c2.run_id = r.run_id AND c2.worker = r.worker AND c2.ordinal = r.ordinal "
        "WHERE a.run_id = ? AND a.role = 'in' AND c1.frame_id != c2.frame_id "
        "AND c1.frame_id LIKE 'app.py:use#%'",
        (run_id,),
    ).fetchall()
    assert matches == [("app.py:use#1", "items", "app.py:make#1")]


def test_first_calls_is_a_setting(tmp_path):
    connection, run_id = run(tmp_path, "first_calls = 2\nlast_calls = 1\n")
    detailed = connection.execute(
        "SELECT COUNT(*) FROM stream_calls WHERE run_id = ? AND frame_id LIKE 'app.py:f#%'", (run_id,)
    ).fetchone()[0]
    assert detailed == 2


def test_an_exhausted_detail_budget_never_stops_the_fingerprints(tmp_path):
    connection, run_id = run(tmp_path, "max_trace_bytes = 1500\nfirst_calls = 100\n")
    fingerprinted = connection.execute(
        "SELECT COUNT(*) FROM stream_fp WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    detailed = connection.execute(
        "SELECT COUNT(*) FROM stream_calls WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    assert fingerprinted == 22  # make, use and 20 calls of f
    assert 0 < detailed < 22
