"""Deterministic tests for the recorder's elapsed and active intervals."""

import pytest

from ldr import tracer
from ldr.diff import node_hash
from ldr.node import Node
from ldr.render import render_inspection


@pytest.fixture
def timed_program(tmp_path, monkeypatch):
    now = 0

    def advance(ms):
        nonlocal now
        now += ms * 1_000_000

    monkeypatch.setattr(tracer, "perf_counter_ns", lambda: now)
    functions = {"advance": advance}
    source = """
def child():
    advance(3)

def parent():
    advance(2)
    child()
    advance(5)

def generator():
    advance(2)
    try:
        yield 1
    except ValueError:
        advance(4)
    else:
        advance(4)

def first_caller(iterator):
    advance(1)
    next(iterator)
    advance(2)

def second_caller(iterator):
    advance(2)
    next(iterator, None)
    advance(1)

async def coroutine(pause):
    advance(3)
    await pause
    advance(5)
"""
    exec(compile(source, str(tmp_path / "program.py"), "exec"), functions)  # noqa: S102
    recorder = tracer.Recorder([str(tmp_path)])
    recorder.start()
    try:
        yield recorder, functions, advance
    finally:
        recorder.stop()


def timing(elapsed, own, children=0, suspended=0, *, complete=True):
    return {
        "elapsed_ms": elapsed,
        "own_ms": own,
        "children_ms": children,
        "suspended_ms": suspended,
        "complete": complete,
    }


def test_own_time_excludes_traced_child_and_round_trips(timed_program):
    recorder, functions, _ = timed_program
    functions["parent"]()
    (parent,) = recorder.roots
    assert parent.timing == timing(10, 7, children=3)
    assert parent.children[0].timing == timing(3, 3)
    restored = Node.from_dict(parent.to_dict())
    assert restored.ms == 10
    assert restored.timing == parent.timing
    assert "children 3.00" in render_inspection([restored], restored.frame_id)
    restored.timing = timing(20, 20)
    assert node_hash(restored) == node_hash(parent)


def test_generator_throw_resumes_timing(timed_program):
    recorder, functions, advance = timed_program
    call = functions["generator"]()
    next(call)
    advance(20)
    with pytest.raises(StopIteration):
        call.throw(ValueError("resume"))
    assert recorder.roots[0].timing == timing(26, 6, suspended=20)


def test_coroutine_wait_is_suspended_time(timed_program):
    class Pause:
        def __await__(self):
            return iter([None])

    recorder, functions, advance = timed_program
    call = functions["coroutine"](Pause())
    call.send(None)
    advance(40)
    with pytest.raises(StopIteration):
        call.send(None)
    assert recorder.roots[0].timing == timing(48, 8, suspended=40)


def test_resumed_child_time_belongs_to_the_current_caller(timed_program):
    recorder, functions, advance = timed_program
    call = functions["generator"]()
    functions["first_caller"](call)
    advance(20)
    functions["second_caller"](call)

    first, second = recorder.roots
    assert first.timing == timing(5, 3, children=2)
    assert second.timing == timing(7, 3, children=4)
    assert first.children[0].timing == timing(30, 6, suspended=24)


def test_stopped_generator_timing_is_incomplete(timed_program):
    recorder, functions, advance = timed_program
    call = functions["generator"]()
    next(call)
    advance(20)
    recorder.stop()
    call.close()
    assert recorder.roots[0].timing == timing(22, 2, suspended=20, complete=False)
