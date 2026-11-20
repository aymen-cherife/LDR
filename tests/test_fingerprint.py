"""Value fingerprints: comparison must read the full value, not the preview."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldr.diff import diff_tree  # noqa: E402
from ldr.render import render_diff  # noqa: E402
from ldr.tracer import Recorder  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def record(fn):
    rec = Recorder(scope=[FIX])
    rec.start()
    try:
        fn()
    finally:
        rec.stop()
    return rec


def test_change_beyond_the_preview_is_reported():
    """Defect 1: items 1-10 vs the same list with item 8 changed.

    Both previews read ``[0, 1, 2, 3, 4, 5, ...]``, so comparing the preview
    reported no difference and the consumer got blamed."""
    import fixtures.values as v

    before = list(range(10))
    after = list(range(10))
    after[7] = 99
    old = record(lambda: v.consume(before))
    new = record(lambda: v.consume(after))
    assert old.roots[0].in_ == new.roots[0].in_  # previews really are identical

    changes = diff_tree(old.roots[0], new.roots[0])

    assert changes, "a change at item 8 of 10 must not read as 'unchanged'"
    text = render_diff(changes)
    assert "consume" in text
    assert "beyond the preview" in text


def test_old_schema_snapshot_compares_as_unknown_with_a_rerecord_hint(tmp_path):
    """Schema 6 snapshots hold previews only. They must never read as 'unchanged'."""
    import json
    import subprocess

    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    (tmp_path / "m.py").write_text(
        "def top(x):\n    return x + 1\n"
        "if __name__ == '__main__':\n    print(top(5))\n"
    )

    def run(*extra):
        return subprocess.run(
            [sys.executable, "-m", "ldr.cli", "run", str(tmp_path / "m.py"),
             "--only", str(tmp_path), *extra],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )

    assert run("--golden").returncode == 0
    snapshot = next((tmp_path / ".ldr" / "snapshots").glob("*.json"))
    payload = json.loads(snapshot.read_text())
    payload["schema_version"] = 6
    snapshot.write_text(json.dumps(payload))

    result = run("--diff", "--fail-on-change")

    assert "No behavioural change" not in result.stdout
    assert "unknown" in result.stdout.lower()
    assert "re-record" in result.stdout.lower()
    assert result.returncode == 3


class Hooked:
    """Every hook LDR could trip over counts how often it ran."""

    calls = []

    def __init__(self, **state):
        self.__dict__.update(state)

    def __repr__(self):
        Hooked.calls.append("__repr__")
        return "<Hooked>"

    def __str__(self):
        Hooked.calls.append("__str__")
        return "hooked"

    def __getattr__(self, name):
        Hooked.calls.append(f"__getattr__:{name}")
        raise AttributeError(name)

    def __getstate__(self):
        Hooked.calls.append("__getstate__")
        return {}

    def __eq__(self, other):
        Hooked.calls.append("__eq__")
        return self is other

    def __hash__(self):
        Hooked.calls.append("__hash__")
        return 1

    def __lt__(self, other):
        Hooked.calls.append("__lt__")
        return False

    def __iter__(self):
        Hooked.calls.append("__iter__")
        return iter(())

    def __len__(self):
        Hooked.calls.append("__len__")
        return 0

    @property
    def derived(self):
        Hooked.calls.append("derived")
        return 1


class HookedDict(dict):
    def items(self):
        Hooked.calls.append("items")
        return super().items()

    def __iter__(self):
        Hooked.calls.append("dict.__iter__")
        return super().__iter__()

    def __repr__(self):
        Hooked.calls.append("dict.__repr__")
        return "{}"


class Slotted:
    __slots__ = ("size", "label")

    def __init__(self, size, label):
        self.size = size
        self.label = label

    @property
    def size_text(self):
        Hooked.calls.append("size_text")
        return str(self.size)


def test_recording_objects_never_runs_user_code():
    """Defect 2: repr, str, properties, __getattr__, __getstate__, __hash__,
    __eq__, __iter__ and __len__ can mutate, block or do work. None may run."""
    import fixtures.values as v
    from ldr.node import capture, capture_state

    plain = Hooked(total=6)
    nested = {"k": [Hooked(a=1)], "s": {Hooked(b=2)}, "t": (Hooked(c=3),)}
    hooked_dict = HookedDict(a=Hooked(d=4))
    slotted = Slotted(3, "x")
    Hooked.calls.clear()  # building the fixtures above is the test's own business

    rec = record(lambda: v.consume([plain, nested, hooked_dict, slotted]))
    capture(plain)
    capture_state(plain)
    capture(nested)
    capture(hooked_dict)
    capture(slotted)

    assert Hooked.calls == []
    assert rec.roots[0].in_fp["items"] not in ("unsupported", "failed")


def test_object_preview_is_structural():
    from ldr.node import capture

    assert capture(Slotted(3, "x")) == "Slotted(size=3, label='x')"
    assert capture(Hooked(total=6, name="cart")) == "Hooked(total=6, name='cart')"
    many = Hooked(**{f"f{i}": "y" * 60 for i in range(30)})
    text = capture(many)
    assert "more fields)" in text, text  # what was cut is always printed
    assert "api_key=<secret: by name, 11 chars>" in capture(Hooked(api_key="abc12345678", mode="x"))


def test_builtin_previews_read_exactly_as_before():
    from ldr.node import capture

    assert capture({"b": 2, "a": 1}) == "{'a': 1, 'b': 2}"
    assert capture(list(range(10))) == "[0, 1, 2, 3, 4, 5, ...]"
    assert capture({3, 1, 2}) == "{1, 2, 3}"
    assert capture(("x", None, 1.5)) == "('x', None, 1.5)"


def test_objects_with_different_state_are_different_and_equal_state_is_equal():
    import fixtures.values as v

    old = record(lambda: v.accept(Slotted(3, "x")))
    same = record(lambda: v.accept(Slotted(3, "x")))
    other = record(lambda: v.accept(Slotted(4, "x")))

    assert diff_tree(old.roots[0], same.roots[0]) == []
    assert diff_tree(old.roots[0], other.roots[0])


def test_budgets_make_oversized_values_unknown_not_slow_and_not_equal():
    """Defect 3, as changed by Phase 2 rule 5: only the depth guard (or an explicit
    node budget) makes a value `unknown`. A large value is walked, not skipped."""
    import time

    from ldr.fingerprint import Budget, fingerprint, is_digest

    assert is_digest(fingerprint(list(range(1000))))
    assert is_digest(fingerprint(list(range(2_000_000))))
    assert fingerprint(list(range(100)), Budget(max_nodes=10)) == "unknown"
    assert is_digest(fingerprint(list(range(5)), Budget(max_nodes=10)))
    assert fingerprint([[[[1]]]], Budget(max_depth=2)) == "unknown"
    assert not hasattr(Budget(), "max_seconds")  # no time budget: see the next test

    deep = 1
    for _ in range(10_000):
        deep = [deep]
    started = time.perf_counter()
    assert fingerprint(deep) == "unknown"
    assert time.perf_counter() - started < 0.5


def test_a_fingerprint_never_depends_on_machine_load(monkeypatch):
    """A time budget would make the same value `complete` in one run and
    `unknown` in the next, faking differences. Only node count and depth bound it."""
    import time

    from ldr.fingerprint import fingerprint, is_digest

    value = {"rows": [list(range(20)) for _ in range(400)]}
    expected = fingerprint(value)
    assert is_digest(expected)

    ticks = iter(range(0, 10**9, 10**6))  # a clock that jumps a million seconds per call
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks))

    assert fingerprint(value) == expected


def test_unknown_values_are_reported_as_unknown_never_unchanged():
    import fixtures.values as v

    def nested(leaf):
        value = [leaf]
        for _ in range(40):  # deeper than the guard
            value = [value]
        return value

    deep_a = nested(1)
    deep_b = nested(2)
    old = record(lambda: v.consume(deep_a))
    new = record(lambda: v.consume(deep_b))
    assert old.roots[0].in_fp["items"] == "unknown"

    changes = diff_tree(old.roots[0], new.roots[0])

    assert [c.kind for c in changes] == ["unknown"]
    text = render_diff(changes)
    assert "cannot be compared" in text and "items" in text


def test_capturing_a_huge_list_does_not_walk_it():
    import time

    from ldr.node import capture

    huge_list = list(range(2_000_000))
    huge_set = set(range(300_000))
    huge_dict = {i: i for i in range(300_000)}
    started = time.perf_counter()
    assert capture(huge_list) == "[0, 1, 2, 3, 4, 5, ...]"
    assert capture(huge_set).endswith(", ...}")
    assert capture(huge_dict).endswith(", ...}")
    # reprlib sorted the whole set and dict (~0.1-0.2 s each) for a 6-item preview
    assert time.perf_counter() - started < 0.05


def test_receiver_state_is_part_of_the_comparison():
    """Defect 4: `self` is not an argument preview, yet same arguments on an
    object in a different state is a different call."""
    import fixtures.values as v

    old = record(lambda: v.Account(10).label("acct"))
    same = record(lambda: v.Account(10).label("acct"))
    other = record(lambda: v.Account(99).label("acct"))
    def calls(rec):
        return [n for r in rec.roots for n in r.walk() if n.name.endswith("label")][0]

    assert "self" not in calls(old).in_  # previews are unchanged
    assert diff_tree(calls(old), calls(same)) == []
    changes = diff_tree(calls(old), calls(other))
    assert changes, "same arguments, different receiver state, must not read as unchanged"
    text = render_diff(changes)
    assert "self" in text and "receiver" in text


def test_receiver_with_unreadable_parts_is_marked_partial_not_dropped():
    import threading

    import fixtures.values as v

    rec = record(lambda: v.Account(10, guard=threading.Lock()).label("acct"))
    node = [n for r in rec.roots for n in r.walk() if n.name.endswith("label")][0]
    token = node.in_fp["self"]
    assert token.endswith("+") and token[:-1].isalnum(), token  # readable part only


def test_dict_order_is_kept_but_set_order_is_not():
    """Defect 13: insertion order is part of a dict's value; sets have none."""
    import fixtures.values as v

    from ldr.fingerprint import fingerprint

    assert fingerprint({"a": 1, "b": 2}) != fingerprint({"b": 2, "a": 1})
    assert fingerprint({1, 2, 3}) == fingerprint({3, 2, 1})
    assert fingerprint(["a", "b"]) != fingerprint(["b", "a"])

    old = record(lambda: v.accept({"a": 1, "b": 2}))
    new = record(lambda: v.accept({"b": 2, "a": 1}))
    assert old.roots[0].in_ == new.roots[0].in_  # previews stay sorted, as before

    changes = diff_tree(old.roots[0], new.roots[0])

    assert changes
    assert "changed beyond the preview or in ordering" in render_diff(changes)


def test_set_fingerprints_do_not_depend_on_hash_randomization():
    """String hashes differ per process, so set iteration order does too. An
    order-sensitive set fingerprint would flag a difference between two runs of
    identical code. Dict order, by contrast, is a stable part of the value."""
    import subprocess

    probe = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from ldr.fingerprint import fingerprint\n"
        "words = {'alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'}\n"
        "print(fingerprint(words), fingerprint(frozenset(words)),\n"
        "      fingerprint({'k': words, 'n': [frozenset(words)]}),\n"
        "      fingerprint({'a': 1, 'b': 2}), fingerprint({'b': 2, 'a': 1}))\n"
    )
    root = os.path.dirname(HERE)

    def run(seed):
        return subprocess.run(
            [sys.executable, "-c", probe, root],
            env=dict(os.environ, PYTHONHASHSEED=seed),
            capture_output=True, text=True, check=True,
        ).stdout.split()

    first, second, third = run("1"), run("2"), run("12345")

    assert first == second == third
    set_fp, frozen_fp, nested_fp, ab_fp, ba_fp = first
    assert set_fp != frozen_fp  # a set and a frozenset are different values
    assert ab_fp != ba_fp  # dict order is kept, in every process


def test_equal_partial_receivers_are_equal_in_readable_parts_not_equal():
    """Two receivers that differ only in an unreadable part (a lock) have equal
    `+` digests. That is not proof of equality, and the output must not say it."""
    import threading

    import fixtures.values as v

    held = threading.Lock()
    held.acquire()

    def calls(rec):
        return [n for r in rec.roots for n in r.walk() if n.name.endswith("label")][0]

    free = record(lambda: v.Account(10, guard=threading.Lock()).label("acct"))
    locked = record(lambda: v.Account(10, guard=held).label("acct"))
    assert calls(free).in_fp["self"] == calls(locked).in_fp["self"]  # the lock is unreadable

    changes = diff_tree(calls(free), calls(locked))

    assert [c.kind for c in changes] == ["equal-readable"]
    text = render_diff(changes)
    assert "equal in the readable parts" in text
    assert "not compared" in text
    assert "no behavioural change" not in text.lower()


def test_cli_does_not_report_no_change_for_partially_readable_receivers(tmp_path):
    import json
    import subprocess

    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    script = (
        "import threading\n"
        "class Client:\n"
        "    def __init__(self):\n"
        "        self.name = 'c'\n"
        "        self.guard = threading.Lock()\n"
        "    def call(self, x):\n"
        "        return x\n"
        "client = Client()\n"
        "%s"
        "client.call(1)\n"
    )

    def run(*extra):
        return subprocess.run(
            [sys.executable, "-m", "ldr.cli", "run", "app.py", *extra],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )

    (tmp_path / "app.py").write_text(script % "")
    assert run("--golden").returncode == 0
    (tmp_path / "app.py").write_text(script % "client.guard.acquire()\n")

    result = run("--diff", "--fail-on-change")

    assert "EQUAL IN READABLE PARTS ONLY" in result.stdout
    assert "No behavioural change" not in result.stdout
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["behavior"]["changed"] is False
    assert payload["behavior"]["equal_in_readable_parts_only"] >= 1
    assert result.returncode == 0  # not a proven change; stated, not failed


def test_identical_long_values_are_equal():
    import fixtures.values as v

    old = record(lambda: v.consume(list(range(500))))
    new = record(lambda: v.consume(list(range(500))))
    assert diff_tree(old.roots[0], new.roots[0]) == []
