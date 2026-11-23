"""Close-list item 4: the node ceiling and sketches, and reuse of deeply immutable fingerprints."""

import gc
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ldr import fingerprint as fp
from ldr.fingerprint import Budget, SKETCH_MARK, fingerprint, is_digest, is_sketch

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def reset():
    fp.configure(None)
    fp._identity_cache.clear()
    yield
    fp.configure(None)
    fp._identity_cache.clear()


def rows(n=200, width=10):
    return [list(range(width)) for _ in range(n)]


def test_a_value_over_the_ceiling_is_sketched_and_one_under_it_is_walked_in_full():
    fp.configure(500)
    small = [1, 2, 3]
    big = rows()
    assert not is_sketch(fingerprint(small))
    token = fingerprint(big)
    assert is_sketch(token) and is_digest(token) and token.endswith(SKETCH_MARK)
    assert fingerprint(rows()) == token  # deterministic
    assert fingerprint(rows(201)) != token  # the size is part of the sketch
    deep = rows()
    deep[100][5] = -1  # a change below the top-level parts
    assert fingerprint(deep) == token  # ...is what an unchanged sketch cannot see
    fp.configure(None)
    assert not is_sketch(fingerprint(rows())) and fingerprint(deep) != fingerprint(rows())


def test_an_explicit_budget_keeps_its_old_answer_and_lifting_removes_the_ceiling():
    fp.configure(500)
    assert fingerprint(rows(), Budget(max_nodes=500)) == "unknown"
    with fp.lifted():
        assert not is_sketch(fingerprint(rows()))
    assert is_sketch(fingerprint(rows()))


def test_a_sketch_never_contains_a_secret():
    fp.configure(50)
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    token = fingerprint([{secret: 1} for _ in range(100)])
    assert secret not in token
    named = fingerprint([{"api_key": "x", "n": i} for i in range(100)])
    assert named == "redacted" or is_sketch(named)


# --- reuse of deeply immutable fingerprints

def big_tuple(start=0, size=100):
    return tuple(range(start, start + size))


def test_a_deeply_immutable_value_is_reused_between_calls(monkeypatch):
    value = big_tuple()
    first = fp.fingerprint_details(value)
    calls = []
    original = fp._fingerprint_details
    monkeypatch.setattr(fp, "_fingerprint_details", lambda *a, **k: calls.append(1) or original(*a, **k))
    assert fp.fingerprint_details(value) == first and calls == []


def test_the_cache_holds_a_reference_so_an_id_cannot_be_reused_while_cached():
    value = big_tuple()
    before = sys.getrefcount(value)
    fp.fingerprint_details(value)
    assert sys.getrefcount(value) == before + 1  # the cache's own reference
    assert fp._identity_cache[id(value), False][0] is value


def test_a_new_object_with_a_freed_objects_id_is_computed_fresh(monkeypatch):
    monkeypatch.setattr(fp, "IDENTITY_LIMIT", 1)
    old = big_tuple(0)
    old_id, old_token = id(old), fp.fingerprint_details(old)[0]
    fp.fingerprint_details(big_tuple(1000))  # evicts the first entry (the limit is 1)
    assert (old_id, False) not in fp._identity_cache
    del old
    gc.collect()
    fresh = None
    for attempt in range(20_000):  # ask the allocator for the freed id again
        candidate = big_tuple(500_000 + attempt)
        if id(candidate) == old_id:
            fresh = candidate
            break
    if fresh is None:
        pytest.skip("the allocator did not hand the id back")
    token = fp.fingerprint_details(fresh)[0]
    assert token == fp._fingerprint_details(fresh, fp.DEFAULT_BUDGET, False)[0]  # computed fresh
    assert token != old_token


def test_a_stale_entry_for_the_same_id_is_rejected_by_the_identity_check():
    first, second = big_tuple(0), big_tuple(5000)
    fp._identity_cache[id(second), False] = (first, ("not-the-answer", True))  # the wrong object
    assert fp.fingerprint_details(second)[0] != "not-the-answer"


def test_a_tuple_holding_a_list_is_never_reused_and_sees_the_list_change():
    inner = [1, 2, 3]
    value = tuple(range(40)) + (inner,)
    first = fp.fingerprint_details(value)
    assert first[1] is False  # not deeply immutable
    inner.append(4)
    assert fp.fingerprint_details(value)[0] != first[0]
    assert (id(value), False) not in fp._identity_cache


def test_the_identity_cache_is_bounded_and_cleared_when_redaction_inputs_change(monkeypatch):
    monkeypatch.setattr(fp, "IDENTITY_LIMIT", 8)
    keep = [big_tuple(index * 1000) for index in range(40)]
    for value in keep:
        fp.fingerprint_details(value)
    assert len(fp._identity_cache) <= 8
    from ldr import redact

    try:
        redact.load_environment({"RELEASE_TOKEN": "release-candidate-7731"})
        fp.fingerprint_details("x")
        assert fp._identity_cache == {}
    finally:
        redact.load_environment()


# --- in a run

APP = """\
def total(rows):
    return len(rows)


big = [list(range(10)) for _ in range(5000)]
total(big)
"""


def ldr(project, *args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def report(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))["status"]


def configured(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.ldr.storage]\nwalk_ceiling = 1000\n", encoding="utf-8")


def test_the_gaps_line_names_the_value_the_sketch_state_and_the_exact_command(tmp_path):
    configured(tmp_path)
    ldr(tmp_path, "--golden")
    (tmp_path / "app.py").write_text(APP + "# edit\n", encoding="utf-8")
    result = ldr(tmp_path, "--diff")
    assert "too large: list at app.py:total#1 (rows), sketch unchanged" in result.stdout
    assert "An unchanged sketch does not prove the value is unchanged." in result.stdout
    assert "ldr run app.py --uncap=app.py:total" in result.stdout
    assert report(tmp_path)["too_large"]["examples"][0]["sketch"] == "sketch unchanged"


def test_a_deep_change_is_invisible_to_the_sketch_and_uncapping_finds_it(tmp_path):
    configured(tmp_path)
    ldr(tmp_path, "--golden")
    deep = APP.replace("total(big)", "big[3000][4] = -1\ntotal(big)")
    (tmp_path / "app.py").write_text(deep, encoding="utf-8")
    result = ldr(tmp_path, "--diff")
    assert "sketch unchanged" in result.stdout  # the deep change is invisible to the sketch
    again = ldr(tmp_path, "--diff", "--uncap=app.py:total")
    assert "TOO LARGE" not in again.stdout  # normal mode, ceiling lifted for that function
    assert report(tmp_path)["reference"]["different"] == 1


def test_a_change_of_size_changes_the_sketch(tmp_path):
    configured(tmp_path)
    ldr(tmp_path, "--golden")
    (tmp_path / "app.py").write_text(APP.replace("range(5000)", "range(5001)"), encoding="utf-8")
    assert "sketch changed" in ldr(tmp_path, "--diff").stdout


def test_full_has_no_ceiling(tmp_path):
    configured(tmp_path)
    result = ldr(tmp_path, "--full")
    assert "TOO LARGE" not in result.stdout and "too_large" not in report(tmp_path)


def test_a_sketch_is_not_in_the_argument_equals_return_lookup(tmp_path):
    configured(tmp_path)
    ldr(tmp_path)
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        tokens = [row[0] for row in connection.execute("SELECT fp FROM call_lookup")]
    assert not any(token.endswith(SKETCH_MARK) for token in tokens)
