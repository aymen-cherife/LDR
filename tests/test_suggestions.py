"""Behavioral tests for agent-first follow-up ranking and scope recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ldr.cli import _detect_production_roots

ROOT = Path(__file__).resolve().parents[1]


def invoke(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "ldr.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def load_result(cwd: Path) -> tuple[str, dict]:
    result_dir = cwd / ".ldr"
    return (
        (result_dir / "last.txt").read_text(),
        json.loads((result_dir / "last.json").read_text()),
    )


def make_flat_project(project: Path) -> None:
    (project / "app").mkdir()
    (project / "tests").mkdir()
    (project / "app" / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text(
        "[project]\nname='ranking-fixture'\nversion='0'\n"
    )
    (project / "app" / "checkout.py").write_text(
        "def setup_trace_provider(value):\n"
        "    return {'observed': value}\n"
        "class Receipt:\n"
        "    def __init__(self, total):\n"
        "        self.total = total\n"
        "    def to_dict(self):\n"
        "        return {'total': self.total}\n"
        "def calculate_checkout(prices):\n"
        "    setup_trace_provider(len(prices))\n"
        "    subtotal = sum(prices)\n"
        "    discount = 2 if subtotal > 10 else 0\n"
        "    total = subtotal - discount\n"
        "    Receipt(total).to_dict()\n"
        "    return total\n"
    )
    (project / "tests" / "test_checkout.py").write_text(
        "from app.checkout import calculate_checkout\n"
        "def test_checkout_total():\n"
        "    assert calculate_checkout([6, 7]) == 12\n"
    )


def test_ranked_shortcut_finds_completed_production_frame_and_captures_state(tmp_path):
    make_flat_project(tmp_path)

    broad = invoke(tmp_path, "pytest", "tests/test_checkout.py::test_checkout_total")
    assert broad.returncode == 1
    text, payload = load_result(tmp_path)

    assert payload["scope"]["detected_production_roots"] == ["app"]
    assert len(payload["suggestions"]) <= 3
    first = payload["suggestions"][0]
    assert first["function"] == "calculate_checkout"
    assert first["evidence"]["on_failed_branch"] is True
    assert first["evidence"]["identifier_overlap"]
    assert "SUGGESTED INSPECTIONS" in text
    assert f"ldr inspect --frame-id {first['id']}" in text

    focused = invoke(tmp_path, "inspect", "--frame-id", str(first["id"]))
    assert focused.returncode == 1
    focused_text, focused_payload = load_result(tmp_path)
    assert focused_payload["inspection"]["matched"] is True
    assert focused_payload["inspection"]["match_count"] == 1
    state = focused_payload["inspection"]["frames"][0]["state_steps"]
    changed_names = {
        change["name"]
        for step in state
        for change in step["changes"]
    }
    assert {"subtotal", "discount", "total"} <= changed_names
    assert "STATE TRANSITIONS" in focused_text
    assert focused_payload["status"]["timing"]["total_seconds"] > 0


def test_ranking_is_deterministic_and_downranks_diagnostic_noise(tmp_path):
    make_flat_project(tmp_path)

    first_run = invoke(
        tmp_path, "pytest", "tests/test_checkout.py::test_checkout_total"
    )
    assert first_run.returncode == 1
    _, first = load_result(tmp_path)
    second_run = invoke(
        tmp_path, "pytest", "tests/test_checkout.py::test_checkout_total"
    )
    assert second_run.returncode == 1
    _, second = load_result(tmp_path)

    def projection(payload):
        return [
            (item["id"], item["function"], item["score"], item["reasons"])
            for item in payload["suggestions"]
        ]
    assert projection(first) == projection(second)
    assert first["suggestions"][0]["function"] == "calculate_checkout"
    assert [item["function"] for item in first["suggestions"]] == ["calculate_checkout"]


def test_wrong_explicit_scope_warns_and_names_detected_root(tmp_path):
    make_flat_project(tmp_path)

    result = invoke(
        tmp_path,
        "pytest",
        "tests/test_checkout.py::test_checkout_total",
        "--only",
        "src",
        "tests",
    )
    assert result.returncode == 1
    text, payload = load_result(tmp_path)

    assert payload["scope"]["detected_production_roots"] == ["app"]
    assert any("do not exist" in warning for warning in payload["scope"]["warnings"])
    assert any("no frames" in warning for warning in payload["scope"]["warnings"])
    assert "SCOPE WARNING" in text
    assert "Detected production roots: app" in text


def test_detects_setuptools_src_layout_without_importing_target(tmp_path):
    (tmp_path / "src" / "widget").mkdir(parents=True)
    (tmp_path / "src" / "widget" / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\nwhere=['src']\n"
    )

    assert _detect_production_roots(str(tmp_path)) == [str(tmp_path / "src")]


def test_ranked_shortcut_inspects_all_calls_when_repetition_is_evidence(tmp_path):
    (tmp_path / "logic.py").write_text(
        "def transform(value):\n"
        "    adjusted = value * 2\n"
        "    return adjusted\n"
        "def test_transform_sequence():\n"
        "    transform(1)\n"
        "    assert transform(3) == 7\n"
    )

    broad = invoke(tmp_path, "pytest", "logic.py::test_transform_sequence")
    assert broad.returncode == 1
    _, payload = load_result(tmp_path)
    suggestion = next(
        item for item in payload["suggestions"]
        if item["function"] == "transform"
    )
    assert suggestion["match_strategy"] == "all_calls"
    assert suggestion["selector"].endswith("#*")

    first_focus = invoke(tmp_path, "inspect", "--frame-id", str(suggestion["id"]))
    assert first_focus.returncode == 1
    _, focused = load_result(tmp_path)
    assert focused["inspection"]["match_count"] == 2

    second_focus = invoke(tmp_path, "inspect", "--frame-id", str(suggestion["id"]))
    assert second_focus.returncode == 1
    _, repeated = load_result(tmp_path)
    assert repeated["inspection"]["match_count"] == 2


def test_input_mutator_outranks_read_only_predicate_and_shows_evidence(tmp_path):
    (tmp_path / "pipeline.py").write_text(
        "SENTINEL = object()\n"
        "def is_lazy(socket):\n"
        "    return False\n"
        "def write_outputs(inputs, value, socket):\n"
        "    if not is_lazy(socket):\n"
        "        inputs['receiver'] = value\n"
        "def test_existing_value_survives_sentinel():\n"
        "    inputs = {'receiver': 'keep'}\n"
        "    write_outputs(inputs, SENTINEL, object())\n"
        "    assert inputs['receiver'] == 'keep'\n"
    )

    broad = invoke(
        tmp_path,
        "pytest",
        "pipeline.py::test_existing_value_survives_sentinel",
    )
    assert broad.returncode == 1
    text, payload = load_result(tmp_path)

    first = payload["suggestions"][0]
    assert first["function"] == "write_outputs"
    assert first["evidence"]["mutated_inputs"] == ["inputs"]
    assert first["reasons"][0] == (
        "caller-owned input changed in place during call: inputs"
    )
    assert "INPUT MUTATION inputs:" in text
    assert "'keep'" in text
    assert "<object object at 0x*>" in text
