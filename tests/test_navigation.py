"""Strong contracts for the agent-facing evidence surface."""

import json
import os
import subprocess
import sys
from pathlib import Path

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


def test_copyable_command_handles_spaces_and_resolves_from_original_cwd(tmp_path):
    project = tmp_path / "space project"
    project.mkdir()
    (project / "calc.py").write_text("def scale(x):\n    return x * 2\n")
    (project / "test_calc.py").write_text(
        "from calc import scale\ndef test_scale():\n    assert scale(3) == 999\n"
    )

    broad = invoke(
        tmp_path,
        "pytest",
        "space project/test_calc.py",
        "--only",
        str(project),
    )
    assert broad.returncode == 1
    text, payload = load_result(tmp_path)
    command_line = next(
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("Run: ldr inspect ")
    )
    argv = command_line.removeprefix("Run: ").split()

    assert argv[:3] == ["ldr", "inspect", "--frame-id"]
    assert int(argv[3]) in {item["id"] for item in payload["suggestions"]}
    assert payload["rerun"]["target"] == "space project/test_calc.py"
    assert payload["rerun"]["scope"] == ["space project"]

    focused = invoke(tmp_path, *argv[1:])
    assert focused.returncode == 1
    _, focused_payload = load_result(tmp_path)
    assert focused_payload["inspection"]["matched"] is True


def test_sampling_stays_bounded_when_late_inputs_are_all_distinct(tmp_path):
    (tmp_path / "many.py").write_text(
        "def convert(x):\n    return x\n"
        "def test_many():\n"
        "    values = [0] * 8 + list(range(1, 121))\n"
        "    assert [convert(x) for x in values] == []\n"
    )
    result = invoke(
        tmp_path,
        "pytest",
        "many.py::test_many",
        "--only",
        str(tmp_path),
        "--max-calls-per-function",
        "8",
    )
    assert result.returncode == 1
    _, payload = load_result(tmp_path)
    convert_frames = [
        frame
        for frame in payload["frame_index"]["frames"]
        if frame["function"] == "convert"
    ]
    assert len(convert_frames) < 30
    assert payload["status"]["sampled_repetitions"] > 0


def test_broad_output_is_compact_and_does_not_claim_uncaptured_semantics(tmp_path):
    (tmp_path / "fail.py").write_text(
        "def inc(x):\n    return x + 1\n"
        "def test_fail():\n    assert inc(1) == 999\n"
    )
    result = invoke(tmp_path, "pytest", "fail.py::test_fail", "--only", str(tmp_path))
    assert result.returncode == 1
    text, _ = load_result(tmp_path)

    assert "SUGGESTED INSPECTIONS" in text
    assert "not claimed bug locations" in text
    assert "EVENT SEQUENCE" not in text
    assert "VALUE TRANSITIONS" not in text
    assert "INPUT MUTATIONS" not in text
    assert len(text.splitlines()) < 35


def test_capture_limits_only_appear_when_capture_is_incomplete(tmp_path):
    (tmp_path / "late.py").write_text(
        "def leaf(x):\n    return x\n"
        "def fail():\n"
        "    for x in range(200):\n        leaf(x)\n"
        "    raise RuntimeError('late')\n"
        "fail()\n"
    )
    limited = invoke(
        tmp_path,
        "run",
        "late.py",
        "--only",
        str(tmp_path),
        "--max-nodes",
        "10",
    )
    assert limited.returncode == 1
    text, payload = load_result(tmp_path)
    assert "CAPTURE LIMITS:" in text
    assert f"recorded={payload['status']['recorded_nodes']}" in text
    assert "truncated=True" in text

    (tmp_path / "small.py").write_text("raise RuntimeError('small')\n")
    complete = invoke(tmp_path, "run", "small.py", "--only", str(tmp_path))
    assert complete.returncode == 1
    text, _ = load_result(tmp_path)
    assert "CAPTURE LIMITS:" not in text


def test_exact_decimal_is_preserved_in_text_and_json(tmp_path):
    exact = "0.1000000000000000055511151231257827021181583404541015625"
    (tmp_path / "dec.py").write_text(
        "from decimal import Decimal\n"
        "def calc(x):\n    return x * 2\n"
        f"def test_dec():\n    assert calc(Decimal('{exact}')) == Decimal('0.2')\n"
    )
    result = invoke(tmp_path, "pytest", "dec.py::test_dec", "--only", str(tmp_path))
    assert result.returncode == 1
    text, payload = load_result(tmp_path)
    assert exact in text
    assert exact in json.dumps(payload)


def test_wildcard_inspection_returns_each_recorded_invocation(tmp_path):
    (tmp_path / "calls.py").write_text(
        "def convert(x):\n    return x * 2\n"
        "def test_calls():\n"
        "    assert convert(1) == 2\n"
        "    assert convert(2) == 999\n"
    )
    broad = invoke(tmp_path, "pytest", "calls.py::test_calls", "--only", str(tmp_path))
    assert broad.returncode == 1
    _, payload = load_result(tmp_path)
    selector = next(
        function["inspect_selector"]
        for function in payload["frame_index"]["functions"]
        if function["function"] == "convert"
    )

    focused = invoke(
        tmp_path,
        "inspect",
        "calls.py::test_calls",
        "--frame",
        selector,
        "--only",
        str(tmp_path),
    )
    assert focused.returncode == 1
    text, focused_payload = load_result(tmp_path)
    frames = focused_payload["inspection"]["frames"]
    assert focused_payload["inspection"]["match_count"] == 2
    assert [frame["in"]["x"] for frame in frames] == ["1", "2"]
    assert "convert(x=1) -> 2" in text
    assert "convert(x=2) -> 4" in text


def test_focused_text_exposes_exact_parent_and_child_frame_ids(tmp_path):
    (tmp_path / "nested.py").write_text(
        "def leaf(x):\n    return x + 1\n"
        "def middle(x):\n    return leaf(x) * 2\n"
        "def test_nested():\n    assert middle(3) == 999\n"
    )
    broad = invoke(tmp_path, "pytest", "nested.py::test_nested", "--only", str(tmp_path))
    assert broad.returncode == 1
    _, payload = load_result(tmp_path)
    frames = payload["frame_index"]["frames"]
    middle = next(frame for frame in frames if frame["function"] == "middle")
    leaf = next(frame for frame in frames if frame["function"] == "leaf")

    focused = invoke(
        tmp_path,
        "inspect",
        "nested.py::test_nested",
        "--frame",
        middle["frame_id"],
        "--only",
        str(tmp_path),
    )
    assert focused.returncode == 1
    text, _ = load_result(tmp_path)
    assert f"parent: {middle['parent_id']}" in text
    assert f"[{leaf['frame_id']}]" in text
