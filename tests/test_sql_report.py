"""Close-list item 1: a default run builds no tree and its report is read from SQLite."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ldr import tracer
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

MANY = """\
def f(n):
    return n + 1


def g(n):
    return f(n) * 2


for index in range(6000):
    g(index)
"""

FAILS = """\
def f(n):
    return n + 1


def explode(n):
    raise ValueError("boom")


def g(n):
    return f(n) * 2


for index in range(6000):
    g(index)
explode(1)
"""


def ldr(project, *args, expected=0):
    result = processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def report(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))


def test_every_call_of_a_run_over_5000_calls_is_in_the_report(tmp_path):
    (tmp_path / "app.py").write_text(MANY, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    data = report(tmp_path)
    assert data["status"]["recorded_nodes"] == 12000
    assert data["status"]["trace_truncated"] is False and data["status"]["sampled_repetitions"] == 0
    assert data["frame_index"]["total"] == 12000 and data["frame_index"]["truncated"] is False
    functions = {item["function"]: item for item in data["frame_index"]["functions"]}
    assert functions["f"]["recorded_calls"] == 6000 and functions["g"]["recorded_calls"] == 6000
    assert functions["f"]["location"] == "app.py:1"
    assert "12000 project call(s) recorded" in result.stdout
    assert "sampled" not in result.stdout and "node budget" not in result.stdout


def test_a_failure_after_5000_calls_is_still_ranked_and_its_frame_is_listed(tmp_path):
    (tmp_path / "app.py").write_text(FAILS, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py", expected=1)
    data = report(tmp_path)
    assert data["frame_index"]["total"] == 12001
    failing = [frame for frame in data["frame_index"]["frames"] if frame["error"]]
    assert [frame["function"] for frame in failing] == ["explode"] and failing[0]["id"] == 12001
    assert "explode(n=1) ! ValueError: boom" in result.stdout
    assert [item["function"] for item in data["suggestions"]][:1] == ["explode"]
    assert "[12000 recorded calls succeeded]" in result.stdout or "12000 recorded calls succeeded" in result.stdout


def test_without_a_tree_no_node_is_built_but_a_failure_still_gets_its_path(tmp_path):
    source = "def leaf(x):\n    return x\ndef top():\n    for i in range(3000):\n        leaf(i)\n    raise RuntimeError('late')\n"
    namespace = {}
    exec(compile(source, str(tmp_path / "prog.py"), "exec"), namespace)  # noqa: S102
    recorder = tracer.Recorder([str(tmp_path)], build_tree=False)
    recorder.start()
    try:
        try:
            namespace["top"]()
        except RuntimeError:
            pass
    finally:
        recorder.stop()
    assert recorder.n_nodes == 0
    assert [root.name for root in recorder.roots] == ["top"]  # the failing path only
    assert recorder.roots[0].error.startswith("RuntimeError")
