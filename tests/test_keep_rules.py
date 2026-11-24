"""Phase 2 rule 3: detail kept for what can be decided at return, according to the keep rules."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from ldr import processes

REPO = Path(__file__).resolve().parents[1]


def ldr(project, *args, expected=0):
    result = processes.run(
        [sys.executable, "-m", "ldr", *args],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def run(tmp_path, source, settings="", *extra, expected=0):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    if settings:
        (tmp_path / "pyproject.toml").write_text("[tool.ldr.storage]\n" + settings, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py", *extra, expected=expected)
    connection = sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")
    run_id = connection.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    return connection, run_id, result


def kept(connection, run_id, function):
    return {
        row[0]: json.loads(row[1])
        for row in connection.execute(
            "SELECT frame_id, payload FROM stream_calls WHERE run_id = ? AND frame_id LIKE ?",
            (run_id, f"app.py:{function}#%"),
        )
    }


def numbers(addresses, function):
    return sorted(int(address.rsplit("#", 1)[1]) for address in addresses if f":{function}#" in address)


def test_a_new_return_type_or_size_bucket_keeps_detail(tmp_path):
    connection, run_id, _ = run(
        tmp_path,
        "def make(n):\n    return 'x' * n\n\n\n"
        "def kind(n):\n    return [n] if n == 20 else n\n\n\n"
        "for n in range(1, 41):\n    make(n)\n    kind(n)\n",
    )
    sized = kept(connection, run_id, "make")
    # Calls 1-8 are the first N. The size buckets (by bit length) first appear at 16 and 32.
    assert numbers(sized, "make") == [1, 2, 3, 4, 5, 6, 7, 8, 16, 32]
    assert sized["app.py:make#16"]["kept"] == ["new shape"]
    assert numbers(kept(connection, run_id, "kind"), "kind") == [1, 2, 3, 4, 5, 6, 7, 8, 20]


def test_every_exception_keeps_detail(tmp_path):
    connection, run_id, _ = run(
        tmp_path,
        "def risky(n):\n    if n % 10 == 0:\n        raise ValueError(n)\n    return n\n\n\n"
        "for n in range(1, 41):\n    try:\n        risky(n)\n    except ValueError:\n        pass\n",
    )
    rows = kept(connection, run_id, "risky")
    assert numbers(rows, "risky") == [1, 2, 3, 4, 5, 6, 7, 8, 10, 20, 30, 40]
    assert rows["app.py:risky#20"]["kept"] == ["exception"]
    assert rows["app.py:risky#20"]["detail_at"] == "return"


def test_an_argument_changed_by_the_call_is_marked_with_both_fingerprints(tmp_path):
    connection, run_id, _ = run(
        tmp_path,
        "def grow(items):\n    items.append(1)\n    return len(items)\n\n\ngrow([])\ngrow([2])\n",
    )
    payload = kept(connection, run_id, "grow")["app.py:grow#1"]
    assert payload["mutated_in_call"] == ["items"]
    assert payload["entry"]["items"] != payload["exit"]["items"]
    assert payload["input_preview"]["items"] == "[]"  # read at entry, so it is the real entry state








def test_recording_keeps_calls_and_values_past_old_caps(tmp_path):
    source = "def work(value):\n    return value\n\nfor index in range(60):\n    work([index] * 11000)\n"
    db, _, result = run(tmp_path, source, "max_recorded_calls = 50\nmax_trace_bytes = 100\nmax_value_bytes = 50\nfirst_calls = 60\n")
    assert db.execute("SELECT COUNT(*) FROM call_records").fetchone() == (60,)
    assert db.execute("SELECT COUNT(*) FROM stream_values WHERE phase = 'return'").fetchone() == (60,)
    assert "INCOMPLETE" not in result.stdout
