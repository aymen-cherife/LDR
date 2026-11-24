"""Phase 2 rule 5: no node cap, a fast path with identical bytes, temp paths normalized."""

import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ldr import fingerprint as fp
from ldr.fingerprint import Budget, fingerprint, is_digest
from ldr import processes

REPO = Path(__file__).resolve().parents[1]


def temp_path(*parts):
    return os.path.join(tempfile.gettempdir(), *parts)


def test_a_large_value_has_a_fingerprint_not_unknown():
    assert is_digest(fingerprint(list(range(100_000))))
    assert fingerprint(list(range(100_000))) == fingerprint(list(range(100_000)))
    assert fingerprint(list(range(100_000))) != fingerprint(list(range(100_000))[:-1] + [-1])


def test_the_comparison_walk_has_no_separate_depth_cutoff():
    deep = 1
    for _ in range(30):
        deep = [deep]
    assert is_digest(fingerprint(deep))
    assert fingerprint(list(range(100)), Budget(max_nodes=10)) == "unknown"  # an explicit budget


def _values(rng):
    ints = [rng.randint(-10**30, 10**30) for _ in range(200)]
    floats = [rng.choice([0.0, -0.0, 1.5, math.inf, -math.inf, math.nan, 1e-300]) for _ in range(200)]
    return [
        ints,
        tuple(ints),
        floats,
        [rng.random() for _ in range(300)],
        [str(rng.random()) + "é" for _ in range(100)],
        [bool(rng.getrandbits(1)) for _ in range(100)],
        [None] * 80,
        ints[:100] + [1.0] + ints[:5],  # mixed: not eligible for the fast path
        [[rng.randint(0, 9) for _ in range(50)] for _ in range(40)],
        [(index, str(index)) for index in range(100)],
        {str(index): index for index in range(100)},
        [{"id": index, "tags": ["a", "b"]} for index in range(60)],
    ]


def test_the_fast_path_produces_identical_bytes(monkeypatch):
    values = _values(random.Random(7))
    monkeypatch.setattr(fp, "FAST_PATH_MIN", 4)
    fast = [fingerprint(value) for value in values]
    monkeypatch.setattr(fp, "FAST_PATH_MIN", 10**12)
    slow = [fingerprint(value) for value in values]
    assert fast == slow
    assert all(is_digest(token) for token in fast)


def test_the_fast_path_still_redacts_and_reports_a_secret(monkeypatch):
    monkeypatch.setattr(fp, "FAST_PATH_MIN", 4)
    values = ["plain"] * 50 + ["ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"]
    assert fingerprint(values) == "redacted"


def test_paths_under_the_temp_directory_are_normalized():
    first = temp_path("tmpab12cd", "out.csv")
    second = temp_path("tmpzz99yy", "out.csv")
    assert fingerprint(first) == fingerprint(second)
    assert fingerprint(f"wrote {first} ok") == fingerprint(f"wrote {second} ok")
    assert fingerprint([first] * 3) == fingerprint([second] * 3)
    assert fingerprint(first) != fingerprint(os.path.join(os.path.expanduser("~"), "out.csv"))
    assert fingerprint(temp_path("tmpfoo")) == fingerprint(temp_path("tmpbar"))
    # Only a path under the directory counts, not a name that merely starts like it.
    assert fingerprint(tempfile.gettempdir() + "foo") != fingerprint(tempfile.gettempdir() + "bar")


def test_the_real_path_of_the_temp_directory_is_matched_too():
    real = os.path.realpath(tempfile.gettempdir())
    assert fingerprint(os.path.join(real, "tmpab12cd")) == fingerprint(temp_path("tmpzz99yy"))


def test_normalization_is_counted_for_the_gaps_line():
    before = fp.STATS.tmp_normalized
    fingerprint(temp_path("tmp1", "a"))
    fingerprint("no path here")
    assert fp.STATS.tmp_normalized == before + 1


def test_walked_nodes_are_counted_as_the_fingerprint_cost():
    before = fp.STATS.nodes
    fingerprint(list(range(1000)))
    assert fp.STATS.nodes - before >= 1000


def run_script(tmp_path, source):
    (tmp_path / "big.py").write_text(source, encoding="utf-8")
    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "big.py"],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    return report, (tmp_path / ".ldr" / "last.txt").read_text(encoding="utf-8")


def test_a_run_reports_fingerprint_cost_per_function_and_normalized_paths(tmp_path):
    report, text = run_script(
        tmp_path,
        "import os, tempfile\n"
        "def big(items):\n    return len(items)\n"
        "def pathy(path):\n    return path\n"
        "big(list(range(5000)))\n"
        "pathy(os.path.join(tempfile.gettempdir(), 'tmpabc123', 'out.csv'))\n",
    )
    summary = report["status"]["fingerprint"]
    cost = {item["function"]: item["nodes"] for item in summary["cost"]}
    assert cost["big.py:big"] >= 5000
    assert next(iter(summary["cost"]))["function"] == "big.py:big"
    assert summary["tmp_normalized"] >= 1
    assert "<tmp>/…" in text


def test_existing_signatures_match_before_f1():
    import datetime
    import functools

    class Slotted:
        __slots__ = ("item",)

        def __init__(self):
            self.item = [1, "text"]

    Slotted.__module__ = "__main__"
    Slotted.__qualname__ = "Slotted"
    cycle = []
    cycle.append(cycle)
    values = [
        None, False, True, 2**90, -0.0, 1+2j, "a\ud800b", b"data", bytearray(b"data"),
        [1, 2, 3], (1, [2, "x"]), {"b": {2, 1}, "a": frozenset(("y", "z"))},
        cycle, Slotted(), datetime.date(2026, 1, 2), functools.partial(pow, 2, exp=3),
        {"api_key": "synthetic-secret"},
    ]
    expected = [
        "738096e476d3c5447c3cb95c39bfc964",
        "3662d6121c293c1c21dc4d3401650d44",
        "0583525866ce6d4c711b8d319b34a247",
        "6a8e9b098598aa8136749f0a0a8ad819",
        "a81f7a7ee7ae6bb90a1e49d8f2bf28be",
        "d9f22fe501fa6bc5605cde9e4997f84b",
        "f41daf77ae7d4e456bb72a2500031d3a",
        "15e932a57154bf00e77207d21df287b4",
        "941c4e6046f873615a3e36ff3e0febd2",
        "5a4e73a5bb94ed4b11440532215a6d52",
        "bb3ce60f0eecf0590a8332d24d43a2dd",
        "f0a8d092bc3b5a07c5c0621f8257651b",
        "bbf4d489e5714af2668ccb14e694aa4f",
        "1bf4df7b880e7018adbbe80492e4c0d8",
        "5faa8355ab36441793981b5c442555e3",
        "cf19770a32347912497cdaedd06e56ce",
        "redacted",
    ]
    assert [fingerprint(value) for value in values] == expected
