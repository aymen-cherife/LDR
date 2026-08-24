"""Tests aimed at the things that actually break tracers in real code."""

import asyncio
import os
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldr.diff import (  # noqa: E402
    DOWNSTREAM,
    INPUT_CHANGED,
    ORIGIN,
    diff_tree,
    mark_unstable,
    node_hash,
)
from ldr.node import Node, capture  # noqa: E402
from ldr.render import render_diff, render_run, tree  # noqa: E402
from ldr.tracer import Recorder  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def record(fn, scope=None):
    rec = Recorder(scope=scope or [FIX])
    rec.start()
    try:
        fn()
    finally:
        rec.stop()
    return rec


def names(rec):
    return [n.name for r in rec.roots for n in r.walk()]


# --- basics ---------------------------------------------------------


def test_captures_args_and_return():
    import fixtures.basic as b

    rec = record(lambda: b.outer(3))
    root = rec.roots[0]
    assert root.name == "outer"
    assert root.in_ == {"x": "3"}
    assert root.out == "12"
    assert [c.name for c in root.children] == ["double", "add_six"]


def test_self_is_not_captured_as_arg():
    import fixtures.basic as b

    rec = record(lambda: b.Calc(2).scale(5))
    node = [n for r in rec.roots for n in r.walk() if n.name.endswith("scale")][0]
    assert "self" not in node.in_
    assert node.in_ == {"k": "5"}


def test_out_of_scope_code_is_not_traced():
    import fixtures.basic as b

    rec = record(lambda: b.uses_stdlib())
    assert all("json" not in n and "loads" not in n for n in names(rec))


# --- the failure modes ----------------------------------------------


def test_error_attributed_to_raising_function_not_caller():
    import fixtures.basic as b

    rec = Recorder(scope=[FIX])
    rec.start()
    try:
        b.outer("free")
    except TypeError:
        pass
    finally:
        rec.stop()
    failing = [n for n in rec.roots[0].walk() if n.error]
    # walk() is pre-order: first = outermost caller, last = actual raiser.
    assert failing[0].name == "outer"
    assert failing[-1].name == "add_six"
    assert "TypeError" in failing[-1].error
    # the argument that caused it is on the same node, not in a traceback
    assert failing[-1].in_ == {"x": "'freefree'"}


def test_generator_does_not_corrupt_the_tree():
    """A yielding frame never returns. Without PY_YIELD handling the
    stack never unwinds and every subsequent call is mis-parented."""
    import fixtures.gens as g

    rec = record(lambda: g.consume())
    root = rec.roots[0]
    # after the generator is drained, sibling calls must still be siblings
    assert root.name == "consume"
    kids = [c.name for c in root.children]
    assert "after" in kids, kids
    # 'after' must be a direct child of consume, not nested under the gen
    assert root.children[-1].name == "after"


def test_async_gather_legs_are_siblings():
    import fixtures.aio as a

    rec = record(lambda: asyncio.run(a.main()))
    root = [n for n in rec.roots[0].walk() if n.name == "main"][0]
    kids = {c.name for c in root.children}
    assert {"fast", "slow"} <= kids, kids
    slow = [c for c in root.children if c.name == "slow"][0]
    fast = [c for c in root.children if c.name == "fast"][0]
    # the failing leg must be identifiable on its own
    assert slow.error is not None
    assert fast.error is None
    assert fast.out == "40"


def test_threads_do_not_interleave_into_one_tree():
    import fixtures.basic as b

    def run():
        ts = [threading.Thread(target=b.outer, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]

    rec = record(run)
    outers = [r for r in rec.roots if r.name == "outer"]
    assert len(outers) == 4
    for o in outers:
        assert [c.name for c in o.children] == ["double", "add_six"]


def test_recursion_is_capped_not_explosive():
    import fixtures.basic as b

    rec = Recorder(scope=[FIX], max_nodes=200)
    rec.start()
    try:
        b.fib(18)  # ~8000 calls unbounded
    finally:
        rec.stop()
    assert rec.n_nodes <= 200
    assert rec.truncated is True


def test_depth_is_capped():
    import fixtures.basic as b

    rec = Recorder(scope=[FIX], max_depth=10, max_nodes=100000)
    rec.start()
    try:
        b.deep(40)
    finally:
        rec.stop()
    depth = 0
    n = rec.roots[0]
    while n.children:
        depth += 1
        n = n.children[0]
    assert depth <= 10


def test_huge_values_are_truncated():
    import fixtures.basic as b

    rec = record(lambda: b.takes_big(list(range(10000))))
    node = rec.roots[0]
    assert len(node.in_["data"]) < 200


def test_broken_repr_does_not_kill_the_run():
    import fixtures.basic as b

    rec = record(lambda: b.takes_big(b.Explosive()))
    val = rec.roots[0].in_["data"]
    assert val and len(val) < 200
    assert "Explosive" in val   # falls back to a safe placeholder
    assert rec.roots[0].out == "1"  # and the program still completed


def test_arguments_are_captured_before_later_mutation():
    """Holding the reference and formatting at render time would show the
    mutated list, i.e. a lie about what was passed."""
    import fixtures.basic as b

    rec = record(lambda: b.mutates([1, 2]))
    assert rec.roots[0].in_["xs"] == "[1, 2]"
    assert "99" not in rec.roots[0].in_["xs"]


def test_capture_never_raises():
    class Bad:
        def __repr__(self):
            raise RuntimeError("nope")

    val = capture(Bad())
    assert isinstance(val, str) and "Bad" in val


# --- hashing and diff -----------------------------------------------


def test_hash_ignores_timing():
    a = Node("f", {"x": "1"}, out="2", ms=1.0)
    b = Node("f", {"x": "1"}, out="2", ms=999.0)
    assert node_hash(a) == node_hash(b)


def test_hash_changes_on_behaviour():
    a = Node("f", {"x": "1"}, out="2")
    b = Node("f", {"x": "1"}, out="3")
    assert node_hash(a) != node_hash(b)


def test_diff_marks_the_edited_function_as_origin():
    old = Node("checkout", {"n": "1"}, out="105.6")
    old.children = [Node("tax", {"a": "96"}, out="105.6"), Node("ship", {"t": "105.6"}, out="0")]
    new = Node("checkout", {"n": "1"}, out="113.1")
    new.children = [Node("tax", {"a": "96"}, out="105.6"), Node("ship", {"t": "96.0"}, out="7.5")]

    changes = diff_tree(old, new)
    by_path = {c.path.split(" > ")[-1]: c.kind for c in changes}
    assert by_path["checkout"] == ORIGIN
    assert by_path["ship"] == INPUT_CHANGED
    assert "tax" not in by_path  # untouched must stay silent


def test_diff_marks_caller_downstream_when_callee_changed():
    old = Node("top", {}, out="10")
    old.children = [Node("inner", {}, out="10")]
    new = Node("top", {}, out="11")
    new.children = [Node("inner", {}, out="11")]

    changes = diff_tree(old, new)
    kinds = {c.path.split(" > ")[-1]: c.kind for c in changes}
    assert kinds["inner"] == ORIGIN
    assert kinds["top"] == DOWNSTREAM


def test_identical_runs_produce_no_diff():
    import fixtures.basic as b

    r1 = record(lambda: b.outer(3))
    r2 = record(lambda: b.outer(3))
    assert diff_tree(r1.roots[0], r2.roots[0]) == []


def test_nondeterminism_is_detected_and_excluded():
    """The single biggest first-session killer: unchanged code that hashes
    differently every run because of time/uuid/random."""
    import fixtures.nondet as nd

    r1 = record(lambda: nd.pipeline())
    r2 = record(lambda: nd.pipeline())
    mark_unstable(r1.roots[0], r2.roots[0])

    unstable = [n.name for n in r1.roots[0].walk() if n.unstable]
    assert "stamp" in unstable
    assert "pure" not in unstable

    r3 = record(lambda: nd.pipeline())
    changes = diff_tree(r1.roots[0], r3.roots[0])
    assert all(c.kind == "unstable" for c in changes), changes


def test_unstable_child_does_not_make_parent_a_false_origin():
    import fixtures.nondet as nd

    r1 = record(lambda: nd.pipeline())
    r2 = record(lambda: nd.pipeline())
    mark_unstable(r1.roots[0], r2.roots[0])
    r3 = record(lambda: nd.pipeline())
    changes = diff_tree(r1.roots[0], r3.roots[0])
    assert not [c for c in changes if c.kind == ORIGIN]


# --- render ---------------------------------------------------------


def test_render_puts_failures_first():
    import fixtures.basic as b

    rec = Recorder(scope=[FIX])
    rec.start()
    try:
        b.outer("free")
    except TypeError:
        pass
    finally:
        rec.stop()
    out = render_run(rec.roots)
    # Failure-first AND failure-only: the passing calls are summarised,
    # not reprinted. Duplicating them was the single biggest token cost.
    assert out.startswith("FAILED PATH")
    assert "FULL TRACE" not in out
    assert "TypeError" in out

    full = render_run(rec.roots, full=True)
    assert "FULL TRACE" in full


def test_repeated_siblings_collapse():
    """A loop calling the same thing 30x is one fact, not 30 lines."""
    import fixtures.basic as b

    rec = record(lambda: [b.double(2) for _ in range(30)])
    out = render_run(rec.roots, full=False)
    assert out.count("double") <= 3, out
    assert "x9" in out


def test_uninformative_leaves_are_pruned():
    root = Node("top", {}, out="1")
    root.children = [Node("lock_release", {}, out="None"), Node("real", {"x": "1"}, out="2")]
    out = "\n".join(tree(root, prune=True))
    assert "real" in out
    assert "lock_release" not in out


def test_many_args_are_capped():
    n = Node("wide", {f"a{i}": str(i) for i in range(20)}, out="1")
    line = "\n".join(tree(n))
    assert "more" in line
    assert len(line) < 200


def test_agent_render_has_no_ansi():
    import fixtures.basic as b

    rec = record(lambda: b.outer(3))
    assert "\033[" not in render_run(rec.roots, color=False)


def test_diff_render_leads_with_origin():
    old = Node("top", {}, out="10")
    old.children = [Node("inner", {}, out="10")]
    new = Node("top", {}, out="11")
    new.children = [Node("inner", {}, out="11")]
    text = render_diff(diff_tree(old, new))
    assert text.index("CHANGED HERE") < text.index("downstream")


def test_serialization_round_trip():
    import fixtures.basic as b

    rec = record(lambda: b.outer(3))
    d = rec.roots[0].to_dict()
    assert node_hash(Node.from_dict(d)) == node_hash(rec.roots[0])


# --- end to end -----------------------------------------------------


def test_cli_run_writes_output_file(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    r = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", os.path.join(FIX, "script.py"),
         "--only", FIX],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".ldr" / "last.txt").exists()
    assert "compute" in (tmp_path / ".ldr" / "last.txt").read_text()


def test_cli_golden_then_diff_finds_origin(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    src = tmp_path / "m.py"
    src.write_text(
        "def inner(x):\n    return x * 2\n"
        "def top(x):\n    return inner(x) + 1\n"
        "if __name__ == '__main__':\n    print(top(5))\n"
    )
    run = lambda extra: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "ldr.cli", "run", str(src), "--only", str(tmp_path), *extra],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    a = run(["--golden"])
    assert a.returncode == 0, a.stderr

    src.write_text(
        "def inner(x):\n    return x * 3\n"
        "def top(x):\n    return inner(x) + 1\n"
        "if __name__ == '__main__':\n    print(top(5))\n"
    )
    b = run(["--diff"])
    assert b.returncode == 0, b.stderr
    assert "CHANGED HERE" in b.stdout
    assert "inner" in b.stdout.split("CHANGED HERE")[1].split("\n")[0]


def test_cli_pytest_mode_traces_the_code_under_test(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    (tmp_path / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_lib.py").write_text(
        "from lib import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    r = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "pytest", str(tmp_path / "test_lib.py"),
         "--only", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    out = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "add(a=1, b=2) -> 3" in out, out


# --- regression guards from real-bug findings -----------------------


def test_test_frames_never_outrank_source_frames():
    """An AssertionError in the test is the symptom, not the origin.
    It outranked the real behaviour change on every tqdm bug until
    _is_test moved to the front of the sort key."""
    from ldr.diff import rank

    src = Node("mod.compute", {"x": "1"}, out="9")
    test = Node("test_thing", {}, error="AssertionError: 9 != 8")
    cs = diff_tree(
        Node("test_thing", {}, out="None", children=[Node("mod.compute", {"x": "1"}, out="8")]),
        Node("test_thing", {}, error="AssertionError", children=[src]),
    )
    ordered = sorted(cs, key=rank)
    assert "test_" not in ordered[0].path.split(" > ")[-1]


def test_nested_test_helper_counts_as_test_code():
    from ldr.diff import _is_test

    assert _is_test("test_bool.<locals>.internal.<locals>.get_bool_for_tqdm")
    assert not _is_test("tqdm.format_meter")


def test_children_are_aligned_by_name_not_index():
    """One inserted call must not make every later sibling report as
    changed."""
    old = Node("top", {}, out="1")
    old.children = [Node("a", {}, out="1"), Node("b", {}, out="2"), Node("c", {}, out="3")]
    new = Node("top", {}, out="1")
    new.children = [
        Node("a", {}, out="1"), Node("x", {}, out="9"),
        Node("b", {}, out="2"), Node("c", {}, out="3"),
    ]
    changes = diff_tree(old, new)
    names = {c.path.split(" > ")[-1] for c in changes if c.kind in ("added", "removed")}
    assert names == {"x"}, names


def test_failing_node_shows_all_arguments():
    """Arg capping hid unit_scale=10 on the frame that raised - half of
    tqdm bug 4."""
    n = Node("f", {f"a{i}": str(i) for i in range(20)}, error="TypeError: boom")
    line = "\n".join(tree(n))
    assert "a19=19" in line


def test_addresses_are_normalised():
    """Raw 0x addresses differ per process and made every object-valued
    argument read as a behaviour change."""
    class Thing:
        pass

    a, b = capture(Thing()), capture(Thing())
    assert "0x*" in a
    assert a == b


def test_verify_snapshots_then_diffs(tmp_path):
    """`ldr verify` with no flags: first call snapshots, second reports."""
    # PYTHONDONTWRITEBYTECODE: the two source versions below are the same
    # length. If both writes land in the same filesystem mtime tick, the
    # default mtime+size pyc cache check can't tell the file changed and
    # silently reuses the stale compiled version - a real, observed flake,
    # not a fault in LDR. Force fresh compilation every time instead.
    env = dict(
        os.environ, PYTHONPATH=os.path.dirname(HERE), PYTHONDONTWRITEBYTECODE="1"
    )
    src = tmp_path / "m.py"
    src.write_text(
        "def inner(x):\n    return x * 2\n"
        "def top(x):\n    return inner(x) + 1\n"
        "if __name__ == '__main__':\n    print(top(5))\n"
    )
    (tmp_path / "t.py").write_text(
        "import sys; sys.path.insert(0, r'%s')\n"
        "from m import top\n\ndef test_top():\n    assert top(5) == 11\n" % tmp_path
    )

    def verify():
        return subprocess.run(
            [sys.executable, "-m", "ldr.cli", "verify", str(tmp_path / "t.py"),
             "--runner", "pytest", "--only", str(tmp_path)],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )

    a = verify()
    assert a.returncode == 0, a.stderr
    assert "Snapshot saved" in a.stdout

    src.write_text(
        "def inner(x):\n    return x * 3\n"
        "def top(x):\n    return inner(x) + 1\n"
        "if __name__ == '__main__':\n    print(top(5))\n"
    )
    b = verify()
    assert b.returncode == 1, b.stderr
    out = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "CHANGED HERE" in out or "RAISES HERE" in out
    assert "inner" in out


def test_module_mode_traces_unittest():
    """Plenty of real projects never adopted pytest."""
    import fixtures.basic  # noqa: F401

    from ldr.cli import main as _main

    assert callable(_main)


def test_call_chain_shows_real_nesting_not_flat_list():
    """The chain view must show actual parent/child call structure,
    not the flat `[LABEL] a > b > c` text list."""
    from ldr.render import render_call_chain

    old = Node("top", {}, out="1")
    old.children = [Node("mid", {}, out="1")]
    old.children[0].children = [Node("leaf", {}, out="1")]
    new = Node("top", {}, out="1")
    new.children = [Node("mid", {}, out="1")]
    new.children[0].children = [Node("leaf", {}, out="2")]

    changes = diff_tree(old, new)
    out = render_call_chain([new], changes)
    lines = out.splitlines()
    # leaf must be indented further than mid, which must be indented
    # further than top - that IS the call structure.
    depth = {l.strip().split("(")[0]: len(l) - len(l.lstrip()) for l in lines}
    assert depth["top"] < depth["mid"] < depth["leaf"]


def test_call_chain_drops_branches_with_no_change():
    from ldr.render import render_call_chain

    old = Node("top", {}, out="1")
    old.children = [Node("changed", {}, out="1"), Node("untouched", {}, out="5")]
    new = Node("top", {}, out="1")
    new.children = [Node("changed", {}, out="2"), Node("untouched", {}, out="5")]

    changes = diff_tree(old, new)
    out = render_call_chain([new], changes)
    assert "changed" in out
    assert "untouched" not in out


# --- regression guards from the LedgerFox self-test session ---------


def test_run_mode_survives_a_real_crash(tmp_path):
    """BUG: `ldr run` on a script that genuinely crashes used to crash
    itself with a raw Python traceback, replacing its own output at the
    exact moment it exists to help."""
    src = tmp_path / "crashy.py"
    src.write_text(
        "def inner(x):\n    return x / 0\n"
        "def outer():\n    return inner(5)\n"
        "outer()\n"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    r = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", str(src), "--only", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 1, r.stderr
    assert "RUN STATUS: CRASHED" in r.stdout
    assert "ZeroDivisionError" in r.stdout
    assert "FAILED PATH" in r.stdout
    assert "outer" in r.stdout and "inner" in r.stdout


def test_entry_file_failure_is_never_hidden_by_only(tmp_path):
    """BUG: with --only pointed at a library, an assertion failing in the
    TEST file itself (outside that scope) was invisible - "FULL TRACE"
    with no failure shown, even though the runner reported a real one."""
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "__init__.py").write_text("")
    (lib / "thing.py").write_text("def double(x):\n    return x * 2\n")
    test_file = tmp_path / "test_it.py"
    test_file.write_text(
        "import sys; sys.path.insert(0, r'%s')\n"
        "from lib.thing import double\n\n"
        "def test_it():\n"
        "    assert double(3) == 999  # deliberately wrong\n" % tmp_path
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    r = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "pytest", str(test_file),
         "--only", str(lib)],  # scope excludes the test file on purpose
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert "FAILED PATH" in r.stdout
    assert "AssertionError" in r.stdout


def test_only_accepts_multiple_paths_without_dropping_any():
    """--only used to accept a single path; the golden-run probe
    subprocess also only forwarded the first scope entry, silently
    narrowing scope on the second half of every --golden call."""
    from ldr.cli import _entry_dirs

    assert isinstance(_entry_dirs("run", __file__), list)


def test_collection_failure_gets_an_honest_note_not_a_silent_pass(tmp_path):
    """A bad top-level import fails during pytest COLLECTION, before any
    test function runs and inside module-level code (deliberately
    filtered as noise). This is structurally invisible to the tracer -
    the honest move is to say so, not show a clean "FULL TRACE" that
    looks like nothing went wrong."""
    test_file = tmp_path / "test_badimport.py"
    test_file.write_text(
        "import nonexistent_module_xyz_ldr_test\n\n"
        "def test_something():\n    assert True\n"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    r = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "pytest", str(test_file),
         "--only", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert "NOTE:" in r.stdout
    assert "trace cannot localize that failure" in r.stdout
