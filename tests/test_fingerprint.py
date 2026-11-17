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
    assert "api_key=<redacted>" in capture(Hooked(api_key="abc12345678", mode="x"))


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


def test_identical_long_values_are_equal():
    import fixtures.values as v

    old = record(lambda: v.consume(list(range(500))))
    new = record(lambda: v.consume(list(range(500))))
    assert diff_tree(old.roots[0], new.roots[0]) == []
