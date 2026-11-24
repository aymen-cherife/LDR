"""Phase 2 rules 6, 7 and 8: hash seed, random-number state and test order."""

import json
import os
import random
import sqlite3
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from ldr import reproduce
from ldr.pytest_capture import RuntimeCapture

REPO = Path(__file__).resolve().parents[1]


def ldr(project, *args, env=None, expected=0):
    environment = dict(os.environ, PYTHONPATH=str(REPO))
    environment.pop("PYTHONHASHSEED", None)
    environment.update(env or {})
    result = processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project, env=environment,
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def report(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))


# --- rule 6

def test_the_users_hash_seed_is_kept_and_anything_else_is_chosen():
    assert reproduce.choose_hash_seed({"PYTHONHASHSEED": "7"}) == ("7", "user")
    assert reproduce.choose_hash_seed({"PYTHONHASHSEED": "0"}) == ("0", "user")
    for environ in ({}, {"PYTHONHASHSEED": "random"}, {"PYTHONHASHSEED": ""}):
        value, source = reproduce.choose_hash_seed(environ)
        assert source == "chosen" and 0 <= int(value) < 2**32
    assert reproduce.choose_hash_seed({"PYTHONHASHSEED": "7"}, recorded="99") == ("99", "recorded")
    assert reproduce.choose_hash_seed({}, recorded=0) == ("0", "recorded")


APP_SEED = """\
import os
from ldr import processes
print("SEED", os.environ.get("PYTHONHASHSEED"), os.environ.get("LDR_HASH_SEED_SOURCE"))
print("ORDER", list({"alpha", "beta", "gamma", "delta", "epsilon"}))
"""


def test_the_target_runs_with_the_recorded_seed_and_never_sees_the_marker(tmp_path):
    (tmp_path / "app.py").write_text(APP_SEED, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    seed_line = next(line for line in result.stdout.splitlines() if line.startswith("SEED"))
    _, value, marker = seed_line.split()
    assert marker == "None"
    recorded = report(tmp_path)["status"]["reproducibility"]["hash_seed"]
    assert recorded == {"value": value, "source": "chosen"}
    assert "hash seed %s (chosen)" % value in result.stdout


def test_a_user_seed_is_respected_and_gives_the_same_order_twice(tmp_path):
    (tmp_path / "app.py").write_text(APP_SEED, encoding="utf-8")
    first = ldr(tmp_path, "run", "app.py", env={"PYTHONHASHSEED": "5"})
    second = ldr(tmp_path, "run", "app.py", env={"PYTHONHASHSEED": "5"})
    order = lambda text: next(line for line in text.splitlines() if line.startswith("ORDER"))  # noqa: E731
    assert order(first.stdout) == order(second.stdout)
    assert report(tmp_path)["status"]["reproducibility"]["hash_seed"] == {"value": "5", "source": "user"}
    ldr(tmp_path, "run", "app.py", env={"PYTHONHASHSEED": "random"})
    assert report(tmp_path)["status"]["reproducibility"]["hash_seed"]["source"] == "chosen"


# --- rule 7

def test_random_state_round_trips_and_reading_it_does_not_advance_it():
    random.seed(1234)
    before = reproduce.capture_random()
    assert reproduce.capture_random() == before
    expected = [random.random() for _ in range(3)]
    reproduce.restore_random(before)
    assert [random.random() for _ in range(3)] == expected


STUB_NUMPY = """\
class Array(list):
    def tolist(self):
        return list(self)

def array(values, dtype=None):
    return Array(values)

class _Random:
    state = ("MT19937", Array(range(5)), 3, 0, 0.0)
    def get_state(self):
        return self.state
    def set_state(self, state):
        self.state = state

random = _Random()
"""


@pytest.fixture
def stub_numpy(tmp_path, monkeypatch):
    package = tmp_path / "stubs" / "numpy"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(STUB_NUMPY, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "stubs"))
    monkeypatch.delitem(sys.modules, "numpy", raising=False)
    yield
    sys.modules.pop("numpy", None)


def test_numpy_is_read_right_after_it_is_imported_and_not_changed(stub_numpy):
    seen = []
    hook = reproduce.install_numpy_hook(seen.append)
    try:
        assert seen == []  # not imported yet: nothing to read
        import numpy

        assert len(seen) == 1
        assert numpy.random.state == ("MT19937", [0, 1, 2, 3, 4], 3, 0, 0.0)
    finally:
        hook.remove()


def test_a_recorded_numpy_state_is_restored_at_the_same_point(stub_numpy):
    seen = []
    hook = reproduce.install_numpy_hook(seen.append)
    try:
        import numpy

        record = seen[0]
    finally:
        hook.remove()
        sys.modules.pop("numpy")
    numpy_after = []
    hook = reproduce.install_numpy_hook(numpy_after.append, restore=record)
    try:
        import numpy

        numpy.random.state = None  # would show if the later read ran before the restore
    finally:
        hook.remove()
    assert numpy_after == [record]


def test_numpy_imported_before_the_hook_is_read_at_once(stub_numpy):
    import numpy  # noqa: F401

    seen = []
    hook = reproduce.install_numpy_hook(seen.append)
    try:
        assert len(seen) == 1
    finally:
        hook.remove()


def fake_item(nodeid, randomly=None):
    plugins = {"randomly"} if randomly is not None else set()
    config = types.SimpleNamespace(
        pluginmanager=types.SimpleNamespace(hasplugin=lambda name: name in plugins),
        getoption=lambda name: randomly,
    )
    return types.SimpleNamespace(nodeid=nodeid, config=config)


class FakeStore:
    def __init__(self):
        self.tests, self.rng = [], {}

    def save_tests(self, worker, rows):
        self.tests.extend((worker, *row) for row in rows)

    def save_rng(self, name, payload):
        self.rng[name] = payload


def test_a_rerun_restores_the_recorded_random_state_at_each_test():
    random.seed(99)
    wanted = reproduce.capture_random()
    expected = random.random()
    capture = RuntimeCapture(recorder=None)
    capture.store = FakeStore()
    capture.rerun_random = {"t.py::test_b": wanted}
    random.seed(1)
    capture.note_test(fake_item("t.py::test_a"))
    capture.note_test(fake_item("t.py::test_b"))
    assert random.random() == expected
    capture.flush_tests()
    assert [(row[1], row[2]) for row in capture.store.tests] == [(1, "t.py::test_a"), (2, "t.py::test_b")]


def test_pytest_randomly_owns_seeding_when_it_is_active():
    random.seed(5)
    state = random.getstate()
    capture = RuntimeCapture(recorder=None)
    capture.store = FakeStore()
    capture.rerun_random = {"t.py::test_a": reproduce.capture_random()}
    random.seed(6)
    untouched = random.getstate()
    capture.note_test(fake_item("t.py::test_a", randomly=4242))
    assert random.getstate() == untouched and untouched != state  # neither read nor restored
    capture.flush_tests()
    assert capture.store.rng == {"pytest-randomly": "4242"}
    assert capture.store.tests == [("main", 1, "t.py::test_a", None)]
    assert reproduce.randomly_arguments(4242) == ["-p", "randomly", "--randomly-seed=4242"]
    assert reproduce.randomly_arguments(None) == []


# --- rule 8, end to end

TESTS = """\
import random

def test_one():
    assert random.random() < 1

def test_two():
    assert random.random() < 1

def test_three():
    assert random.random() < 1
"""


def test_the_test_order_and_random_state_are_recorded_per_test(tmp_path):
    (tmp_path / "test_things.py").write_text(TESTS, encoding="utf-8")
    result = ldr(tmp_path, "pytest", "test_things.py")
    info = report(tmp_path)["status"]["reproducibility"]
    assert info["tests"] == {"recorded": 3, "order": "recorded"}
    assert info["random"] == {"owner": "ldr", "state": "recorded per test"}
    assert info["numpy"] == "not imported"
    assert "order of 3 test(s) recorded" in result.stdout and "Not controlled:" in result.stdout
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        rows = connection.execute(
            "SELECT position, node_id, random_state FROM run_tests ORDER BY position"
        ).fetchall()
    assert [row[:2] for row in rows] == [
        (1, "test_things.py::test_one"), (2, "test_things.py::test_two"), (3, "test_things.py::test_three"),
    ]
    assert all(row[2] for row in rows)
    # Each recorded state restores to a working generator.
    reproduce.restore_random(rows[0][2])
    assert 0 <= random.random() < 1


def test_each_worker_records_its_own_order(tmp_path):
    pytest.importorskip("xdist")
    (tmp_path / "test_things.py").write_text(TESTS, encoding="utf-8")
    ldr(tmp_path, "pytest", "test_things.py", "--", "-n", "2")
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        rows = connection.execute("SELECT worker, position, node_id FROM run_tests").fetchall()
    assert sorted(row[2] for row in rows) == sorted(f"test_things.py::test_{n}" for n in ("one", "two", "three"))
    assert {row[0] for row in rows} <= {"gw0", "gw1"}
    assert sorted(position for worker, position, _ in rows if worker == "gw0") == list(
        range(1, 1 + sum(1 for row in rows if row[0] == "gw0"))
    )


def test_a_script_run_records_the_seed_but_has_no_test_order(tmp_path):
    (tmp_path / "app.py").write_text("print('x')\n", encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    info = report(tmp_path)["status"]["reproducibility"]
    assert "tests" not in info and info["hash_seed"]["source"] == "chosen"
