"""Phase 2 rule 3: detail kept for what can be decided at return, with caps and a drop order."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def ldr(project, *args, expected=0):
    result = subprocess.run(
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


def test_the_normal_budget_drops_first_n_detail_before_keep_rules(tmp_path):
    source = (
        "def steady(n):\n    return n\n\n\n"
        "def boom(n):\n    raise ValueError(n)\n\n\n"
        "for n in range(300):\n    steady(n % 3)\n"
        "for n in range(3):\n    try:\n        boom(n)\n    except ValueError:\n        pass\n"
    )
    connection, run_id, result = run(
        tmp_path, source, "max_trace_bytes = 1200\nhard_cap_bytes = 40000\nfirst_calls = 500\n"
    )
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    dropped = report["status"]["call_stream"]["dropped"]
    assert dropped["first N"] > 0 and "exception" not in dropped
    assert numbers(kept(connection, run_id, "boom"), "boom") == [1, 2, 3]
    (used,) = connection.execute(
        "SELECT used_bytes FROM stream_budget WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert used <= 40000
    assert "DETAIL DROPPED" in result.stdout and "first N" in result.stdout


def test_the_hard_cap_drops_lower_priority_detail_first(tmp_path):
    source = (
        "def boom(n):\n    raise ValueError(n)\n\n\n"
        "def make(n):\n    return 'x' * n\n\n\n"
        "for n in range(60):\n    try:\n        boom(n)\n    except ValueError:\n        pass\n"
        "for n in range(1, 400):\n    make(n)\n"
    )
    connection, run_id, _ = run(
        tmp_path, source, "max_trace_bytes = 800\nhard_cap_bytes = 4000\nfirst_calls = 8\n"
    )
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    dropped = report["status"]["call_stream"]["dropped"]
    (used,) = connection.execute(
        "SELECT used_bytes FROM stream_budget WHERE run_id = ?", (run_id,)
    ).fetchone()
    # Exceptions may use the space up to the hard cap. New-shape detail stops at half of it
    # (rank 2 of 4), so it is dropped while exception detail is still being kept.
    assert 800 + 3200 * 2 // 4 < used <= 4000
    assert dropped.get("new shape", 0) > 0 and dropped.get("exception", 0) > 0


def test_passing_the_fingerprint_ceiling_stops_the_run_and_says_so(tmp_path):
    source = "def f(n):\n    return n\n\n\nfor n in range(200):\n    f(n)\n"
    connection, run_id, result = run(
        tmp_path, source, "max_recorded_calls = 50\n", "--golden"
    )
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    incomplete = report["status"]["incomplete"]
    assert "50" in incomplete
    assert connection.execute(
        "SELECT COUNT(*) FROM stream_fp WHERE run_id = ?", (run_id,)
    ).fetchone()[0] <= 50
    assert "INCOMPLETE" in result.stdout
    assert report["snapshot"]["action"] == "unknown"
    assert not Path(report["snapshot"]["path"]).exists()
