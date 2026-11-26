"""Byte parity for the optional signature prototype."""
import decimal
import functools
import hashlib
import random

import pytest

from ldr import fingerprint as fp

try:
    from ldr import _native as native
except ModuleNotFoundError as error:
    if error.name != "ldr._native":
        raise
    pytest.skip("native module not built", allow_module_level=True)
except ImportError:
    # A missing extension is optional. An existing extension that fails to load is a failure.
    import importlib.util
    if importlib.util.find_spec("ldr._native") is not None:
        raise
    pytest.skip("native module not built", allow_module_level=True)


class Base:
    __slots__ = ("base", "unset")


class Slots(Base):
    __slots__ = ("__private", "__dict__")

    def __init__(self):
        self.base = 1
        self.__private = 2
        self.extra = 3


def result(value, native_reader, budget=fp.DEFAULT_BUDGET, partial=False):
    walk = fp._Walk(budget, partial)
    try:
        if native_reader:
            digest = native.encode(walk, value)
        else:
            out = hashlib.blake2b(digest_size=16)
            walk.encode(value, out)
            digest = out.hexdigest()
        return bytes.fromhex(digest), walk.partial, walk.immutable
    except fp._Stop as stop:
        return stop.status


def test_native_matches_python():
    cycle = [1]
    cycle.append(cycle)
    obj = Slots()
    obj.extra = obj
    values = [None, True, False, 0, -(2**200), 2**300, -0.0, float("nan"),
              float("inf"), 1j, "\ud800", "/tmp/example", b"bytes", bytearray(b"mutable"),
              decimal.Decimal("1.20"), range(4), Ellipsis, NotImplemented,
              cycle, obj, {"a": 1, "b": 2}, {3, 1, "a"}, frozenset((1, 2)),
              functools.partial(pow, 2), {"api_key": "secret"}, ["ok"] * 50]
    rng = random.Random(0)
    for _ in range(100):
        values.append([rng.randrange(-100, 100), {"x": rng.random()}, tuple(range(rng.randrange(50)))])
    for value in values:
        for partial in (False, True):
            assert result(value, True, partial=partial) == result(value, False, partial=partial)


def test_native_ceiling_and_bulk_redaction():
    values = [list(range(100)), ["ok"] * 50, ["ok"] * 49 + ["api_key=not-for-storage"]]
    for value in values:
        for limit in (1, 20, 50, 100, 101):
            budget = fp.Budget(limit)
            assert result(value, True, budget) == result(value, False, budget)


def test_native_explicit_stack():
    value = 0
    # Keep every level alive so destruction is independent of the walk test.
    levels = [value]
    for _ in range(100_000):
        value = [value]
        levels.append(value)
    assert result(value, True) == result(value, False)


def test_layout_changes_and_descriptors():
    class Layout:
        pass

    class Descriptor:
        def __get__(self, instance, owner):
            raise AssertionError("descriptor executed")

    value = Layout()
    value.a = 1
    assert result(value, True) == result(value, False)
    Layout.x = Descriptor()
    value.b = 2
    assert result(value, True) == result(value, False)


def generated_value(rng, depth):
    if depth == 0:
        return rng.choice([None, False, True, rng.randrange(-2**100, 2**100),
                           rng.random(), "texté", "\ud800", b"bytes"])
    children = [generated_value(rng, depth - 1) for _ in range(rng.randrange(6))]
    kind = rng.randrange(6)
    if kind == 0:
        return children
    if kind == 1:
        return tuple(children)
    if kind == 2:
        return {str(index): value for index, value in enumerate(children)}
    if kind == 3:
        return set(rng.sample(range(100), rng.randrange(12)))
    if kind == 4:
        value = Slots()
        value.extra = children
        return value
    value = [children]
    value.append(value)
    return value


@pytest.mark.parametrize("seed", range(40))
def test_generated_values_have_identical_bytes(seed):
    rng = random.Random(seed)
    value = generated_value(rng, 4)
    for budget in (fp.DEFAULT_BUDGET, fp.Budget(20_000), fp.Budget(20)):
        for partial in (False, True):
            assert result(value, True, budget, partial) == result(value, False, budget, partial)


@pytest.mark.parametrize("length", [20_000, 20_001, 100_000])
def test_large_generated_values_have_identical_bytes(length):
    value = list(range(length))
    assert result(value, True) == result(value, False)
    assert result(value, True, fp.Budget(20_000)) == result(value, False, fp.Budget(20_000))


def test_existing_fingerprint_values(tmp_path):
    import os
    import sys
    from pathlib import Path
    from ldr import processes

    root = Path(__file__).resolve().parents[1]
    probe = root / "tests" / "native_probe"
    # sitecustomize is inherited by the existing tests' CLI and seed probes too.
    failures = tmp_path / "parity-failures.txt"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(probe), str(root))),
               LDR_NATIVE_TEST_FAILURES=str(failures))
    completed = processes.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_fingerprint.py",
         "tests/test_fingerprint_rule5.py", "tests/test_per_call_fingerprints.py"],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not failures.exists(), failures.read_text()


@pytest.mark.parametrize("kind", ["ThemeStack", "Segment", "Style", "Text", "Console"])
def test_real_rich_objects_have_identical_bytes(kind):
    from rich.console import Console
    from rich.segment import Segment
    from rich.style import Style
    from rich.text import Text
    from rich.theme import Theme, ThemeStack

    style = Style(color="red", bold=True, link="https://example.com")
    values = {
        "ThemeStack": ThemeStack(Theme({"test": style})),
        "Segment": Segment("évidence", style),
        "Style": style,
        "Text": Text("évidence", style=style),
        "Console": Console(width=80, force_terminal=True),
    }
    value = values[kind]
    for partial in (False, True):
        assert result(value, True, partial=partial) == result(value, False, partial=partial)


def test_native_bulk_path_respects_python_threshold(monkeypatch):
    monkeypatch.setattr(fp, "FAST_PATH_MIN", 4)
    value = ["plain"] * 9 + ["ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"]
    budget = fp.Budget(2)
    assert result(value, True, budget) == result(value, False, budget)

@pytest.mark.parametrize('value', ['plain', 'é\ud800', b'bytes', bytearray(b'bytes'), ['plain'] * 40])
def test_text_is_native(value):
    before = fp.STATS.python_nodes
    assert result(value, True) == result(value, False)
    assert fp.STATS.python_nodes == before

@pytest.mark.parametrize('value', [Slots(), type('Plain', (), {})()])
def test_safe_structural_values_are_native(value):
    before = fp.STATS.python_nodes
    assert result(value, True) == result(value, False)
    assert fp.STATS.python_nodes == before

@pytest.mark.parametrize('value', [type, len, lambda: None, range(3), 1j])
def test_builtin_leaves_are_native(value):
    before = fp.STATS.python_nodes
    assert result(value, True) == result(value, False)
    assert fp.STATS.python_nodes == before

@pytest.mark.parametrize('value', [functools.partial(pow, 2, exp=3), Slots.__init__.__get__(Slots()), ValueError('value')])
def test_native_callable_and_exception_fields(value):
    before=fp.STATS.python_nodes
    assert result(value, True)==result(value,False)
    assert fp.STATS.python_nodes==before


def test_native_walk_state_access_is_independent_of_node_count():
    class CountingWalk(fp._Walk):
        accesses = 0

        def __getattribute__(self, name):
            if name in ('visited', 'active', 'immutable', 'partial'):
                type(self).accesses += 1
            return super().__getattribute__(name)

    walk = CountingWalk(fp.DEFAULT_BUDGET)
    native.encode(walk, list(range(1000)))
    assert CountingWalk.accesses < 20
    assert walk.visited == 1001


def test_native_clears_active_path_on_walk_failure():
    import threading
    value = [threading.Lock()]
    walk = fp._Walk(fp.DEFAULT_BUDGET)
    with pytest.raises(fp._Stop):
        native.encode(walk, value)
    assert walk.active == {}
    assert walk.visited == 2


def test_argument_capture_batches_entry_and_preserves_mutations(monkeypatch):
    """One native entry per argument capture, with fresh mutable exit evidence."""
    from ldr.tracer import Recorder

    class NativeProbe:
        entries = 0

        def encode(self, *args):
            self.entries += 1
            return native.encode(*args)

        def capture(self, *args):
            self.entries += 1
            return native.capture(*args)

    def target(first, second):
        pass

    class Frame:
        f_back = None

        def __init__(self):
            self.f_locals = {"first": [1, 2], "second": {"a": 3}}

    monkeypatch.setattr(fp, "CACHE", False)
    probe = NativeProbe()
    monkeypatch.setattr(fp, "_native", probe)
    recorder = Recorder([])
    frame = Frame()
    entry = recorder._argument_fingerprints(target.__code__, frame, entering=True)
    assert probe.entries == 1
    frame.f_locals["first"].append(4)
    frame.f_locals["second"]["a"] = 5
    probe.entries = 0
    exit_ = recorder._argument_fingerprints(target.__code__, frame)
    assert probe.entries == 1
    assert entry["first"] != exit_["first"]
    assert entry["second"] != exit_["second"]


def test_direct_container_paths_do_not_construct_python_iterators():
    from pathlib import Path
    source = (Path(__file__).parents[1] / 'native/src/lib.rs').read_text()
    items = source[source.index('Task::Items('):source.index('Task::SetItems(', source.index('Task::Items('))]
    assert '.try_iter()' not in items
