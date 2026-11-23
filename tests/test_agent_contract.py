from __future__ import annotations

import json
import os
import subprocess
import sys

from ldr.diff import diff_tree, mark_unstable
from ldr.node import Node, capture, capture_state
from ldr.tracer import Recorder


ROOT = os.path.dirname(os.path.dirname(__file__))


def invoke(tmp_path, *arguments):
    environment = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "ldr.cli", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )


def test_unrelated_targets_have_independent_snapshots(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "test_b.py").write_text("def test_b():\n    assert True\n")
    assert invoke(tmp_path, "verify", "test_a.py").returncode == 0
    assert invoke(tmp_path, "verify", "test_b.py").returncode == 0
    snapshots = list((tmp_path / ".ldr" / "snapshots").glob("*.json"))
    assert len(snapshots) == 2


def test_expected_exception_is_not_a_failed_path(tmp_path):
    (tmp_path / "test_expected.py").write_text(
        "import pytest\n"
        "def reject():\n    raise ValueError('no')\n"
        "def test_reject():\n    with pytest.raises(ValueError):\n        reject()\n"
    )
    result = invoke(tmp_path, "pytest", "test_expected.py")
    assert result.returncode == 0
    output = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "FAILED PATH" not in output
    assert "handled ValueError" in output


def test_failed_test_preserves_exit_and_writes_machine_result(tmp_path):
    (tmp_path / "test_bad.py").write_text(
        "def calculate(x):\n    return x * 2\n"
        "def test_bad():\n    assert calculate(4) == 9\n"
    )
    result = invoke(tmp_path, "pytest", "test_bad.py")
    assert result.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["status"]["exit_code"] == 1
    assert payload["failures"][0]["location"].startswith("test_bad.py:")
    assert "calculate(x=4) -> 8" in (tmp_path / ".ldr" / "last.txt").read_text()


def test_late_exception_survives_node_budget(tmp_path):
    (tmp_path / "late.py").write_text(
        "def leaf(x):\n    return x\n"
        "def fail():\n"
        "    for x in range(200):\n        leaf(x)\n"
        "    raise RuntimeError('late')\n"
        "fail()\n"
    )
    result = invoke(tmp_path, "run", "late.py", "--max-nodes", "10")
    assert result.returncode == 1
    output = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "fail() ! RuntimeError: late" in output
    # Still compact. The bound grew from 30 when COMMIT and SCOPE header lines were added,
    # to 34 for the REPRODUCIBILITY and shared-receiver lines, and to 37 for SLOWDOWN
    # and HOT FUNCTIONS (rules 16 and 17: each one states a fact or a gap).
    assert len(output.splitlines()) < 37
    # A default run is not sampled or capped: every call is in SQLite, so no limits to state.
    assert "CAPTURE LIMITS" not in output


def test_caught_overflow_exception_does_not_outrank_later_failure(tmp_path):
    (tmp_path / "mixed.py").write_text(
        "def hot(x):\n"
        "    if x == 150:\n        raise ValueError('expected')\n"
        "    return x\n"
        "def fail():\n"
        "    for x in range(200):\n"
        "        try:\n            hot(x)\n"
        "        except ValueError:\n            pass\n"
        "    raise RuntimeError('actual')\n"
        "fail()\n"
    )
    result = invoke(tmp_path, "run", "mixed.py", "--max-nodes", "10")
    assert result.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    errors = [failure["error"] for failure in payload["failures"]]
    # The failure that ended the run comes first and is ranked first; the exception that was
    # caught earlier in the same function is still listed, after it.
    assert "RuntimeError: actual" in errors[0]
    assert payload["suggestions"][0]["function"] == "fail"


def test_varargs_kwargs_locations_and_secret_redaction(tmp_path):
    (tmp_path / "values.py").write_text(
        "def combine(prefix, *values, **options):\n"
        "    return prefix, values, options\n"
        "combine('x', 1, 2, mode='strict', api_key='very-secret-value')\n"
    )
    result = invoke(tmp_path, "run", "values.py")
    assert result.returncode == 0
    output = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "values=(1, 2)" in output
    assert "options={'api_key': '<secret: by name, 17 chars>', 'mode': 'strict'}" in output
    assert "very-secret-value" not in output
    assert "@ values.py:1" in output


def test_volatile_parent_does_not_hide_child_regression():
    baseline = Node("root", {}, out="nonce-a", children=[Node("compute", {}, out="10")])
    probe = Node("root", {}, out="nonce-b", children=[Node("compute", {}, out="10")])
    mark_unstable(baseline, probe)
    changed = Node("root", {}, out="nonce-c", children=[Node("compute", {}, out="99")])
    changes = diff_tree(baseline, changed)
    assert baseline.volatile == ["out"]
    # Nothing is left out: the child's change and the parent's (which carries the note).
    assert [(change.kind, change.path) for change in changes] == [
        ("origin", "root > compute"), ("downstream", "root"),
    ] or {(change.kind, change.path) for change in changes} >= {("origin", "root > compute")}
    by_path = {change.path: change for change in changes}
    assert by_path["root > compute"].varied == []
    assert by_path["root"].varied == ["out"]


def test_source_identity_ignores_checkout_display_path():
    old = Node("calculate", {"x": "4"}, out="8", file="/tmp/a/src/calc.py", source_id="src/calc.py")
    new = Node("calculate", {"x": "4"}, out="9", file="/tmp/b/src/calc.py", source_id="src/calc.py")
    changes = diff_tree(old, new)
    assert [(change.kind, change.path) for change in changes] == [("origin", "calculate")]


def test_project_relative_ids_keep_same_basenames_distinct(tmp_path):
    recorder = Recorder(scope=[str(tmp_path)], project_root=str(tmp_path))
    assert recorder._source_id(str(tmp_path / "src" / "common.py")) == "src/common.py"
    assert recorder._source_id(str(tmp_path / "tests" / "common.py")) == "tests/common.py"


def test_named_snapshot_compares_across_checkout_directories(tmp_path):
    for checkout, multiplier in (("a", 2), ("b", 3)):
        root = tmp_path / checkout
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "cross-checkout"\nversion = "0"\n'
        )
        (root / "probe.py").write_text(
            "def calculate(value):\n"
            f"    return value * {multiplier}\n"
            "print(calculate(4))\n"
        )

    baseline = invoke(
        tmp_path,
        "run", "a/probe.py", "--snapshot-name", "cross-checkout", "--golden",
    )
    assert baseline.returncode == 0, baseline.stderr
    compared = invoke(
        tmp_path,
        "run", "b/probe.py", "--snapshot-name", "cross-checkout", "--diff",
    )
    assert compared.returncode == 0, compared.stderr
    output = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "[CHANGED HERE] calculate" in output
    assert "new call" not in output and "call gone" not in output
    assert "@ b/probe.py:1" in output

    snapshot = json.loads(
        (tmp_path / ".ldr" / "snapshots" / "cross-checkout.json").read_text()
    )
    assert snapshot["invocation"]["target"] == "probe.py"
    assert snapshot["roots"][0]["source_id"] == "probe.py"


def test_capture_redacts_named_and_nested_secrets():
    assert capture("hello", "password") == "<secret: by name, 5 chars>"
    rendered = capture({"token": "not-secret-name", "access_token": "hidden", "safe": "shown"})
    assert "hidden" not in rendered
    assert "shown" in rendered


def test_focused_capture_surfaces_nested_boolean_state_without_getters():
    class Label:
        def __init__(self):
            self._visible = True
            self._in_layout = False

    class Tick:
        def __init__(self):
            self.label1 = Label()
            self._visible = True
            self.api_key = "never-render-this"

    rendered = capture_state(Tick(), "tick")
    assert "label1._visible=True" in rendered
    assert "label1._in_layout=False" in rendered
    assert "never-render-this" not in rendered


def test_agent_can_select_a_frame_and_inspect_local_state(tmp_path):
    (tmp_path / "test_state.py").write_text(
        "def calculate(x):\n"
        "    total = x + 1\n"
        "    api_key = 'very-secret-value'\n"
        "    total *= 2\n"
        "    return total\n"
        "\n"
        "def test_calculate():\n"
        "    assert calculate(3) == 9\n"
    )

    baseline = invoke(tmp_path, "pytest", "test_state.py")
    assert baseline.returncode == 1
    first = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    recent = first["failures"][0]["recent_calls"]
    selected = next(call["frame_id"] for call in recent if call["function"] == "calculate")
    assert selected.endswith("calculate#1")

    inspected = invoke(
        tmp_path,
        "inspect",
        "test_state.py",
        "--frame",
        selected,
        "--max-state-steps",
        "20",
    )
    assert inspected.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["inspection"]["matched"] is True
    frame = payload["inspection"]["frames"][0]
    assert frame["frame_id"] == selected
    changes = [
        change
        for step in frame["state_steps"]
        for change in step["changes"]
    ]
    assert {"name": "total", "before": "<unset>", "after": "4"} in changes
    assert {"name": "total", "before": "4", "after": "8"} in changes
    assert any(
        change["name"] == "api_key" and change["after"] == "<secret: by name, 17 chars>"
        for change in changes
    )
    output = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "STATE TRANSITIONS" in output
    assert "very-secret-value" not in output


def test_pytest_collection_does_not_consume_runtime_budget(tmp_path):
    (tmp_path / "helper.py").write_text(
        "def identity(value):\n"
        "    return value\n"
        "for value in range(200):\n"
        "    identity(value)\n"
    )
    (tmp_path / "test_late.py").write_text(
        "import helper\n"
        "def fail_at_runtime():\n"
        "    raise RuntimeError('runtime evidence')\n"
        "def test_late():\n"
        "    fail_at_runtime()\n"
    )

    result = invoke(
        tmp_path,
        "pytest",
        "test_late.py::test_late",
        "--only",
        str(tmp_path),
        "--max-nodes",
        "10",
    )
    assert result.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["status"]["trace_truncated"] is False
    assert any(failure["function"] == "fail_at_runtime" for failure in payload["failures"])
    assert "identity" not in (tmp_path / ".ldr" / "last.txt").read_text()


def test_pytest_report_selects_late_failure_over_runtime_attribute_probes(tmp_path):
    (tmp_path / "test_overflow_report.py").write_text(
        "def __getattr__(name):\n"
        "    raise AttributeError(f'expected probe: {name}')\n"
        "def hot(value):\n"
        "    return value + 1\n"
        "def actual_failure():\n"
        "    raise RuntimeError('decisive late failure')\n"
        "def test_overflow_report():\n"
        "    for value in range(100):\n"
        "        hot(value)\n"
        "    actual_failure()\n"
    )

    result = invoke(
        tmp_path,
        "pytest",
        "test_overflow_report.py::test_overflow_report",
        "--only",
        str(tmp_path),
        "--max-nodes",
        "10",
    )
    assert result.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    unhandled = payload["failures"]
    assert any(
        failure["function"] == "actual_failure"
        and "decisive late failure" in failure["error"]
        for failure in unhandled
    )
    assert not any("expected probe" in failure["error"] for failure in unhandled)
    output = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "actual_failure() ! RuntimeError: decisive late failure" in output


def test_every_repeated_call_is_recorded_and_the_late_causal_call_is_in_the_failing_path(tmp_path):
    (tmp_path / "test_hot_then_cause.py").write_text(
        "def hot(value):\n"
        "    return value + 1\n"
        "def calculate(value):\n"
        "    return value * 2\n"
        "def test_hot_then_cause():\n"
        "    for value in range(1000):\n"
        "        hot(value)\n"
        "    assert calculate(3) == 7\n"
    )

    result = invoke(
        tmp_path,
        "pytest",
        "test_hot_then_cause.py::test_hot_then_cause",
        "--only",
        str(tmp_path),
        "--max-nodes",
        "20",
    )
    assert result.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["status"]["trace_truncated"] is False
    assert payload["status"]["overflow_transitions"] == 0
    assert payload["status"]["sampled_repetitions"] == 0
    assert payload["frame_index"]["total"] == 1002  # the test, 1000 hot calls, calculate
    hot = next(f for f in payload["frame_index"]["functions"] if f["function"] == "hot")
    assert hot["recorded_calls"] == 1000
    calculate = next(
        function
        for function in payload["frame_index"]["functions"]
        if function["function"] == "calculate"
    )
    assert calculate["sample_frame_ids"] == [
        "test_hot_then_cause.py::test_hot_then_cause/test_hot_then_cause.py:calculate#1"
    ]
    assert "calculate(value=3) -> 6" in (tmp_path / ".ldr" / "last.txt").read_text()


def test_sampled_hot_parent_does_not_discard_novel_causal_child(tmp_path):
    (tmp_path / "test_sampled_parent.py").write_text(
        "def noise(value):\n"
        "    return value\n"
        "def calculate(value):\n"
        "    return value * 2\n"
        "def wrapper(value, causal=False):\n"
        "    if causal:\n"
        "        return calculate(value)\n"
        "    return noise(value)\n"
        "def test_sampled_parent():\n"
        "    for value in range(1000):\n"
        "        wrapper(value)\n"
        "    assert wrapper(3, causal=True) == 7\n"
    )

    result = invoke(
        tmp_path,
        "pytest",
        "test_sampled_parent.py::test_sampled_parent",
        "--only",
        str(tmp_path),
        "--max-nodes",
        "30",
    )
    assert result.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    calculate = next(
        function
        for function in payload["frame_index"]["functions"]
        if function["function"] == "calculate"
    )
    assert calculate["sample_frame_ids"] == [
        "test_sampled_parent.py::test_sampled_parent"
        "/test_sampled_parent.py:wrapper#1001/test_sampled_parent.py:calculate#1"
    ]
    assert "calculate(value=3) -> 6" in (tmp_path / ".ldr" / "last.txt").read_text()


def test_focused_state_history_obeys_its_own_budget(tmp_path):
    (tmp_path / "state_loop.py").write_text(
        "def accumulate():\n"
        "    total = 0\n"
        "    for value in range(20):\n"
        "        total += value\n"
        "    return total\n"
        "accumulate()\n"
    )
    first = invoke(tmp_path, "run", "state_loop.py", "--no-prune")
    assert first.returncode == 0
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    selected = next(
        frame["frame_id"]
        for frame in payload["frame_index"]["frames"]
        if frame["function"] == "accumulate"
    )
    second = invoke(
        tmp_path,
        "inspect",
        "state_loop.py",
        "--frame",
        selected,
        "--max-state-steps",
        "3",
    )
    assert second.returncode == 0
    frame = json.loads((tmp_path / ".ldr" / "last.json").read_text())["inspection"]["frames"][0]
    assert len(frame["state_steps"]) == 3
    assert frame["state_truncated"] is True


def test_focused_state_preserves_distinguishing_long_value_suffix(tmp_path):
    prefix = "x" * 120
    (tmp_path / "state_value.py").write_text(
        "def choose():\n"
        f"    value = '{prefix}FIRST'\n"
        f"    value = '{prefix}SECOND'\n"
        "    return value\n"
        "choose()\n"
    )
    broad = invoke(tmp_path, "run", "state_value.py")
    assert broad.returncode == 0
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    selected = next(
        frame["id"]
        for frame in payload["frame_index"]["frames"]
        if frame["function"] == "choose"
    )

    focused = invoke(tmp_path, "inspect", "--frame-id", str(selected))
    assert focused.returncode == 0
    frame = json.loads((tmp_path / ".ldr" / "last.json").read_text())["inspection"]["frames"][0]
    rendered = json.dumps(frame["state_steps"])
    assert "FIRST" in rendered
    assert "SECOND" in rendered


def test_wildcard_inspection_exposes_late_stateful_invocation(tmp_path):
    (tmp_path / "stateful.py").write_text(
        "class Item:\n"
        "    def __init__(self, enabled):\n"
        "        self.enabled = enabled\n"
        "def evaluate(item):\n"
        "    return item.enabled\n"
        "for _ in range(12):\n"
        "    evaluate(Item(True))\n"
        "evaluate(Item(False))\n"
    )
    first = invoke(tmp_path, "run", "stateful.py")
    assert first.returncode == 0
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    selector = next(
        function["inspect_selector"]
        for function in payload["frame_index"]["functions"]
        if function["function"] == "evaluate"
    )
    second = invoke(tmp_path, "inspect", "stateful.py", "--frame", selector)
    assert second.returncode == 0
    inspected = json.loads((tmp_path / ".ldr" / "last.json").read_text())["inspection"]
    assert inspected["match_count"] == 13
    assert any("enabled=False" in frame["in"]["item"] for frame in inspected["frames"])
