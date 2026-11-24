"""Speed step 3: records are staged during the run and moved to indexed tables once."""

import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from ldr import processes

REPO = Path(__file__).resolve().parents[1]
APP = "def make(n):\n    return list(range(n))\n\n\ndef use(items):\n    return len(items)\n\n\nuse(make(3))\n"


def run(tmp_path, source):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env.pop("VIRTUAL_ENV", None)
    return processes.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def counts(tmp_path):
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("call_records", "call_lookup", "call_records_stage", "call_lookup_stage")
        )


def test_after_a_run_the_records_are_in_the_indexed_tables_and_staging_is_empty(tmp_path):
    assert run(tmp_path, APP).returncode == 0
    records, lookup, staged_records, staged_lookup = counts(tmp_path)
    assert (records, staged_records, staged_lookup) == (2, 0, 0)
    assert lookup == 4  # the argument and return of make and of use
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        plan = " ".join(
            str(row) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM call_lookup WHERE run_id = 'x' AND fp = 'y'"
            )
        )
    assert "call_lookup_fp" in plan


def test_a_run_whose_child_died_is_moved_too(tmp_path):
    kill = int(getattr(signal, "SIGKILL", signal.SIGTERM))
    # Wait past the 100 ms flush, so the two calls reach the parent before the kill.
    result = run(tmp_path, APP + f"import os, time\ntime.sleep(0.5)\nos.kill(os.getpid(), {kill})\n")
    assert result.returncode != 0 and "the run died" in result.stderr
    records, _, staged_records, staged_lookup = counts(tmp_path)
    assert (records, staged_records, staged_lookup) == (2, 0, 0)
