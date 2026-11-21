"""Speed step 1: code LDR does not record is switched off at its first event."""

import collections
import sys

import pytest

from ldr import tracer

EVENTS = ("_on_start", "_on_return", "_on_yield", "_on_resume")

LIBRARY = """
def squares(n):
    for i in range(n):
        yield i * i

def helper(x):
    return x + 1
"""

PROJECT = """
def work():
    total = 0
    for _ in range(200):
        for value in squares(3):
            total += helper(value)
    return total
"""


@pytest.fixture
def counted(monkeypatch):
    """Calls into each Recorder callback, split by whether the code is in the project."""
    counts = collections.Counter()

    def wrap(name):
        original = getattr(tracer.Recorder, name)

        def counting(self, code, *rest):
            where = "project" if self._in_scope(code.co_filename) else "foreign"
            counts[name, where, code.co_name] += 1
            return original(self, code, *rest)

        monkeypatch.setattr(tracer.Recorder, name, counting)

    for name in EVENTS:
        wrap(name)
    return counts


def test_foreign_code_is_called_back_once_per_event_and_the_project_is_still_recorded(
    tmp_path, counted, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    namespace = {}
    exec(compile(LIBRARY, str(tmp_path / "elsewhere" / "lib.py"), "exec"), namespace)  # noqa: S102
    exec(compile(PROJECT, str(project / "app.py"), "exec"), namespace)  # noqa: S102
    # Switched-off events must stay off: nothing may bring them back.
    monkeypatch.setattr(
        sys.monitoring, "restart_events", lambda: pytest.fail("restart_events was called")
    )
    recorder = tracer.Recorder([str(project)])
    recorder.start()
    try:
        assert namespace["work"]() == 200 * (1 + 2 + 5)
    finally:
        recorder.stop()

    # The two library functions are called 200 and 600 times; each event reaches
    # them once. (Recorder.stop is LDR's own code and is called once.)
    for name in EVENTS:
        for function in ("squares", "helper"):
            assert counted[name, "foreign", function] <= 1, (name, function)
    # The generator was resumed 800 times, and the project still saw its call.
    assert counted["_on_resume", "foreign", "squares"] == 1
    assert counted["_on_start", "project", "work"] == 1
    (work,) = recorder.roots
    assert work.name == "work"
