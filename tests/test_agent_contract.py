from __future__ import annotations

import json
import os
import subprocess
import sys

from ldr.diff import diff_tree, mark_unstable
from ldr.node import Node, capture
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
    assert len(output.splitlines()) < 30


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
    assert any("RuntimeError: actual" in error for error in errors)
    assert not any("ValueError: expected" in error for error in errors)


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
    assert "options={'api_key': '<redacted>', 'mode': 'strict'}" in output
    assert "very-secret-value" not in output
    assert "@ values.py:1" in output


def test_volatile_parent_does_not_hide_child_regression():
    baseline = Node("root", {}, out="nonce-a", children=[Node("compute", {}, out="10")])
    probe = Node("root", {}, out="nonce-b", children=[Node("compute", {}, out="10")])
    mark_unstable(baseline, probe)
    changed = Node("root", {}, out="nonce-c", children=[Node("compute", {}, out="99")])
    changes = diff_tree(baseline, changed)
    assert baseline.volatile == ["out"]
    assert [(change.kind, change.path) for change in changes] == [("origin", "root > compute")]


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
    assert capture("hello", "password") == "<redacted>"
    rendered = capture({"token": "not-secret-name", "access_token": "hidden", "safe": "shown"})
    assert "hidden" not in rendered
    assert "shown" in rendered
