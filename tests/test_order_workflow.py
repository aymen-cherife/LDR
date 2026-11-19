"""Compare the outputs and evidence of a working app through the LDR CLI."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "order_workflow"
REPO = FIXTURE.parents[2]
EXPORT_KEY = "fixture-export-key-596841"


@pytest.fixture
def project(tmp_path):
    for file in ("app.py", "orders.csv"):
        shutil.copyfile(FIXTURE / file, tmp_path / file)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ldr.storage]\n"
        "max_trace_bytes = 100000\nmax_value_bytes = 10000\n"
        "max_deep_bytes = 10000\ncapture_lines = true\n",
        encoding="utf-8",
    )
    return tmp_path


def launch(project, *args, rate=100, expected_exit=0):
    result = subprocess.run(
        [sys.executable, *args],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO), EXPORT_RATE=str(rate), EXPORT_API_KEY=EXPORT_KEY),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert EXPORT_KEY not in result.stdout + result.stderr
    return result.stdout


def export(stdout, project):
    line, = (line for line in stdout.splitlines() if line.startswith("EXPORT "))
    result = json.loads(line.removeprefix("EXPORT "))
    encoded = zlib.decompress((project / "orders.json.zlib").read_bytes())
    assert hashlib.sha256(encoded).hexdigest() == result.pop("digest")
    assert result == json.loads(encoded)
    return result


def test_cli_preserves_output_and_saves_readable_evidence(project):
    plain = export(launch(project, "app.py"), project)
    assert plain == {"orders": {"A": 400, "B": 375, "C": 50}, "total_cents": 825}
    traced = launch(
        project, "-m", "ldr", "run", "app.py", "--golden",
        "--deep", "make_export", "--deep", "pricing_options",
    )
    assert export(traced, project) == plain
    trace_dir = project / ".ldr"
    last_json = trace_dir / "last.json"
    saved = last_json.read_bytes()
    report = json.loads(saved)
    status = report["status"]
    assert status["deep_capture"]["matched"] == 2
    assert status["deep_capture"]["observed"] == status["deep_capture"]["retained"]
    assert status["call_stream"]["stored"] == status["call_stream"]["started"]
    run_id = status["run_id"]

    with sqlite3.connect(trace_dir / "traces.sqlite3") as conn:
        calls = {
            address: json.loads(payload)
            for address, payload in conn.execute(
                "SELECT frame_id, payload FROM stream_calls WHERE run_id = ?", (run_id,)
            )
        }
    amounts = [address for address in calls if ":line_amount#" in address]
    assert len(amounts) == 3
    assert all(calls[address]["executed_lines"] for address in amounts)
    assert all(calls[address]["timing"]["complete"] for address in amounts)

    show = ("-m", "ldr", "show", "--run-id", run_id, "--json")
    frames = json.loads(launch(project, *show))["frames"]
    assert len(frames) == 2
    for frame in frames:
        address = frame["frame_id"]
        steps = json.loads(launch(project, *show, address))["frame"]["steps"]
        locals_ = dict(steps[-1]["locals"])
        if ":make_export#" in address:
            totals = locals_["totals"]["value"]["items"]
            assert dict(totals) == plain["orders"]
        else:
            assert ":pricing_options#" in address
            assert any(step["event"] == "yield" for step in steps)
            assert locals_["export_key"] == {"status": "redacted"}
            assert locals_["query"] == {"status": "redacted"}
            assert locals_["parameters"] == {"status": "redacted"}
    assert last_json.read_bytes() == saved
    for path in (last_json, trace_dir / "last.txt", trace_dir / "traces.sqlite3"):
        assert EXPORT_KEY.encode() not in path.read_bytes()

    unchanged = launch(project, "-m", "ldr", "run", "app.py", "--diff", "--fail-on-change")
    assert export(unchanged, project) == plain
    assert json.loads(last_json.read_bytes())["behavior"]["changed"] is False

    changed = launch(
        project, "-m", "ldr", "run", "app.py", "--diff", "--fail-on-change",
        rate=120, expected_exit=3,
    )
    assert export(changed, project) == export(launch(project, "app.py", rate=120), project)
    assert json.loads(last_json.read_bytes())["behavior"]["changed"] is True


def test_budget_limits_saved_evidence_and_preserves_output(project):
    (project / "pyproject.toml").write_text(
        "[tool.ldr.storage]\nmax_trace_bytes = 2000\nmax_deep_bytes = 250\n",
        encoding="utf-8",
    )
    plain = export(launch(project, "app.py"), project)
    traced = launch(project, "-m", "ldr", "run", "app.py", "--deep", "make_export")
    assert export(traced, project) == plain

    trace_dir = project / ".ldr"
    status = json.loads((trace_dir / "last.json").read_bytes())["status"]
    deep = status["deep_capture"]
    assert deep["matched"] == 1
    assert deep["retained"] < deep["observed"]
    assert deep["bytes"] <= 250
    stream = status["call_stream"]
    assert 0 < stream["stored"] < stream["started"]
    with sqlite3.connect(trace_dir / "traces.sqlite3") as conn:
        used, = conn.execute(
            "SELECT used_bytes FROM stream_budget WHERE run_id = ?", (status["run_id"],)
        ).fetchone()
    assert 0 < used <= 2000
