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
        project, "-m", "ldr", "run", "app.py",
        "--deep=make_export", "--deep=pricing_options",
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

    unchanged = launch(project, "-m", "ldr", "run", "app.py", "--fail-on-change")
    assert export(unchanged, project) == plain
    assert json.loads(last_json.read_bytes())["behavior"]["changed"] is False

    changed = launch(
        project, "-m", "ldr", "run", "app.py", "--fail-on-change",
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
    traced = launch(project, "-m", "ldr", "run", "app.py", "--deep=make_export")
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
    assert 0 < used <= 4 * 2000  # keep rules may pass the normal budget, up to the hard cap


def test_boundary_capture_records_python_library_calls_without_their_internals(project):
    config = project / "pyproject.toml"
    with config.open("a", encoding="utf-8") as handle:
        handle.write("max_boundary_bytes = 10000\n")
    plain = export(launch(project, "app.py"), project)
    traced = launch(
        project, "-m", "ldr", "run", "app.py",
        "--boundary", "urllib.parse", "--boundary", "json",
    )
    assert export(traced, project) == plain
    status_file = project / ".ldr" / "last.json"
    saved = status_file.read_bytes()
    status = json.loads(saved)["status"]
    boundary = status["boundary_capture"]
    assert boundary["stored"] == boundary["started"]
    assert boundary["unfinished"] == 0
    assert 0 < boundary["bytes"] <= 10000
    show = ("-m", "ldr", "show", "--boundaries", "--run-id", status["run_id"], "--json")
    calls = json.loads(launch(project, *show))["boundaries"]
    assert boundary["stored"] == len(calls)
    assert all(call["caller"]["file"] == "app.py" for call in calls)
    application_calls = [call for call in calls if call["caller"]["frame_id"] is not None]
    assert [(call["module"], call["function"]) for call in application_calls] == [
        ("urllib.parse", "parse_qsl"), ("json", "dumps"), ("json", "dumps")
    ]
    parse, write, _ = application_calls
    assert ":pricing_options#" in parse["caller"]["frame_id"]
    assert parse["result"] == {"status": "redacted"}
    assert dict(parse["arguments"])["qs"] == {"status": "redacted"}
    assert json.loads(write["result"]["value"]) == plain
    page = json.loads(launch(project, *show, "--offset", str(calls.index(write)), "--limit", "1"))
    assert page["boundaries"] == [write]
    assert status_file.read_bytes() == saved
    for file in ("traces.sqlite3", "last.json", "last.txt"):
        assert EXPORT_KEY.encode() not in (project / ".ldr" / file).read_bytes()

    launch(project, "-m", "ldr", "run", "app.py", "--fail-on-change")
    assert json.loads(status_file.read_bytes())["behavior"]["changed"] is False
    assert json.loads(launch(project, "-m", "ldr", "show", "--boundaries", "--json"))[
        "boundaries"
    ] == []


def test_boundary_capture_requires_a_configured_budget(project):
    (project / "app.py").write_text("print('APP RAN')\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py", "--boundary", "json"],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "--boundary requires explicit" in result.stderr
    assert "APP RAN" not in result.stdout


def test_boundary_capture_reports_handled_library_exception_without_its_message(project):
    with (project / "pyproject.toml").open("a", encoding="utf-8") as handle:
        handle.write("max_boundary_bytes = 10000\n")
    (project / "app.py").write_text(
        "import json, os\n"
        "def accepts_json():\n"
        "    try:\n"
        "        json.loads(os.environ['EXPORT_API_KEY'])\n"
        "    except json.JSONDecodeError:\n"
        "        return False\n"
        "assert not accepts_json()\n",
        encoding="utf-8",
    )
    launch(project, "-m", "ldr", "run", "app.py", "--boundary", "json")
    result = launch(project, "-m", "ldr", "show", "--boundaries", "--json")
    call, = json.loads(result)["boundaries"]
    assert call["function"] == "loads"
    assert call["outcome"] == "raised"
    assert call["exception"] == {"status": "complete", "value": "JSONDecodeError"}
    assert dict(call["arguments"])["s"] == {"status": "redacted"}
    assert EXPORT_KEY.encode() not in (project / ".ldr/traces.sqlite3").read_bytes()


def test_boundary_budget_is_shared_across_pytest_workers(project):
    pytest.importorskip("xdist")
    (project / "pyproject.toml").write_text(
        "[tool.ldr.storage]\nmax_trace_bytes = 100000\nmax_boundary_bytes = 850\n",
        encoding="utf-8",
    )
    (project / "test_calls.py").write_text(
        "import urllib.parse\n"
        "def test_first():\n"
        "    assert urllib.parse.parse_qsl('rate=100') == [('rate', '100')]\n"
        "def test_second():\n"
        "    assert urllib.parse.parse_qsl('rate=120') == [('rate', '120')]\n",
        encoding="utf-8",
    )
    launch(
        project, "-m", "ldr", "pytest", "test_calls.py",
        "--boundary", "urllib.parse", "--", "-n", "2",
    )
    report = json.loads((project / ".ldr/last.json").read_bytes())
    status = report["status"]["boundary_capture"]
    assert report["status"]["trace_workers"] == ["gw0", "gw1"]
    assert status["started"] == 2
    assert status["stored"] == 1
    with sqlite3.connect(project / ".ldr/traces.sqlite3") as conn:
        size, = conn.execute("SELECT sum(length(payload)) FROM stream_boundaries").fetchone()
    assert size == status["bytes"] <= 850
