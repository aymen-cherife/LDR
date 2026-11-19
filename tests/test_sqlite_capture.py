"""Exercise the trace store through LDR's normal CLI."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ldr.fingerprint import fingerprint
from ldr.sqlite_store import storage_settings
from ldr.value_capture import capture_value

REPO = Path(__file__).resolve().parents[1]


def invoke(project, *args):
    return subprocess.run(
        [sys.executable, "-m", "ldr", *args],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        check=False,
    )


def test_concurrent_runs_keep_separate_sqlite_traces(tmp_path):
    (tmp_path / "app.py").write_text("def add(a):\n    return a + 1\nadd(2)\n")
    command = [sys.executable, "-m", "ldr", "run", "app.py"]
    options = {"cwd": tmp_path, "env": dict(os.environ, PYTHONPATH=str(REPO))}
    with (
        subprocess.Popen(command, **options) as left,
        subprocess.Popen(command, **options) as right,
    ):
        assert left.wait() == right.wait() == 0

    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        rows = connection.execute(
            "SELECT run_id, worker, count(*) FROM calls GROUP BY run_id, worker"
        ).fetchall()
        assert len(rows) == 2
        assert all(row[1:] == ("main", 1) for row in rows)
        assert connection.execute(
            "SELECT count(*) FROM captured_values"
        ).fetchone() == (1,)


def test_console_encoding_preserves_run_result_and_utf8_report(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    (tmp_path / "app.py").write_text(
        "def label():\n    return '\\u2603'\nlabel()\n", encoding="utf-8"
    )
    result = invoke(tmp_path, "run", "app.py")
    assert result.returncode == 0, result.stderr
    assert "\\u2603" in result.stdout
    assert "\u2603" in (tmp_path / ".ldr" / "last.txt").read_text(encoding="utf-8")


def test_line_capture_requires_an_explicit_budget(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ldr.storage]\ncapture_lines = true\n"
    )
    with pytest.raises(ValueError, match="explicit max_trace_bytes"):
        storage_settings(tmp_path)


def test_structural_values_explain_a_change_outside_the_preview(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ldr.storage]\nmax_trace_bytes = 100000\nmax_value_bytes = 10000\n"
    )
    program = tmp_path / "app.py"
    template = (
        "def update(items, api_token):\n"
        "    items[42] = {changed}\n"
        "    return items\n"
        "update([8] * 60, 'fixture-secret-kept-out')\n"
    )
    program.write_text(template.format(changed=8))
    baseline = invoke(tmp_path, "run", "app.py", "--golden")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    program.write_text(template.format(changed=9))
    compared = invoke(tmp_path, "run", "app.py", "--diff", "--fail-on-change")
    assert compared.returncode == 3, compared.stdout + compared.stderr
    assert "out[42]: 8 -> 9" in compared.stdout

    inspected = invoke(tmp_path, "inspect", "--frame-id", "1")
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    output = json.loads((tmp_path / ".ldr/last.json").read_text())
    values = output["inspection"]["frames"][0]["captured_values"]
    assert values["entry"]["items"]["value"][42] == 8
    assert values["exit"]["items"]["value"][42] == 9
    assert values["return"]["result"]["value"][42] == 9
    assert values["entry"]["api_token"] == {"status": "redacted"}
    assert (
        b"fixture-secret-kept-out"
        not in (tmp_path / ".ldr/traces.sqlite3").read_bytes()
    )


def test_value_budget_keeps_fingerprint_shape_and_preview(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ldr.storage]\nmax_trace_bytes = 100000\nmax_value_bytes = 8\n"
    )
    (tmp_path / "app.py").write_text(
        "def f(value):\n    return value\nf(list(range(50)))\n"
    )
    assert invoke(tmp_path, "run", "app.py").returncode == 0
    assert invoke(tmp_path, "inspect", "--frame-id", "1").returncode == 0
    result = json.loads((tmp_path / ".ldr/last.json").read_text())
    value = result["inspection"]["frames"][0]["captured_values"]["entry"]["value"]
    assert value["status"] == "partial"
    assert value["shape"] == {"type": "list", "length": 50}
    assert value["fingerprint"] == fingerprint(list(range(50)))
    assert value["preview"].startswith("[0, 1,")
    assert "value" not in value


def test_complete_value_preserves_types_order_and_cycles():
    value = {1: (b"abc", {2, 1})}
    cycle = [value]
    cycle.append(cycle)
    _, encoded = capture_value(cycle, fingerprint(cycle), 1000)
    assert json.loads(encoded) == [
        {
            "type": "dict",
            "items": [
                [
                    1,
                    {
                        "type": "tuple",
                        "items": [
                            {"type": "bytes", "hex": "616263"},
                            {"type": "set", "items": [1, 2]},
                        ],
                    },
                ]
            ],
        },
        {"type": "cycle", "distance": 1},
    ]


def test_complete_value_rechecks_nested_secrets_before_serializing():
    value = {"ordinary": "value"}
    prior = fingerprint(value)
    value["api_key"] = "fixture-nested-secret"
    assert capture_value(value, prior, 1000)[1] is None
    assert capture_value(value, fingerprint(value), 1000) == (
        {"status": "redacted"},
        None,
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_line_capture_keeps_late_branch_and_compacts_loop_repetitions(
    tmp_path, enabled
):
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.ldr.storage]\ncapture_lines = {str(enabled).lower()}\nmax_trace_bytes = 100000\n"
    )
    (tmp_path / "app.py").write_text(
        "def run():\n"
        "    output = 0\n"
        "    for i in range(1000):\n"
        "        if i == 999:\n"
        "            output = i\n"
        "    return output\n"
        "run()\n"
    )
    result = invoke(tmp_path, "run", "app.py")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert report["status"]["call_stream"]["capture_lines"] is enabled
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        row = connection.execute("SELECT payload FROM stream_calls").fetchone()
    payload = json.loads(row[0])
    assert payload.get("executed_lines") == ([2, 3, 4, 5, 6] if enabled else None)


def test_pytest_xdist_records_both_workers_and_preserves_failure(tmp_path):
    pytest.importorskip("xdist")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 2'\n"
        "filterwarnings = ['error']\n"
        "[tool.ldr.storage]\nbatch_size = 1\n"
        "capture_lines = true\n"
        "max_trace_bytes = 100000\n"
        "max_value_bytes = 7\n"
    )
    (tmp_path / "calc.py").write_text("def total(a):\n    return a + 1\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import total\n"
        "def test_first():\n    assert total(1) == 2\n"
        "def test_second():\n    assert total(2) == 5\n"
    )

    result = invoke(tmp_path, "pytest", "test_calc.py")
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["status"]["trace_workers"] == ["gw0", "gw1"], (
        result.stdout + result.stderr
    )
    assert payload["scope"]["project_calls"] == 2
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        rows = connection.execute(
            "SELECT worker, parent, payload FROM calls WHERE run_id = ?",
            (payload["status"]["run_id"],),
        ).fetchall()
        streamed_workers = connection.execute(
            "SELECT DISTINCT worker FROM stream_calls WHERE run_id = ? ORDER BY worker",
            (payload["status"]["run_id"],),
        ).fetchall()
        stream_payloads = connection.execute(
            "SELECT payload FROM stream_calls WHERE run_id = ?",
            (payload["status"]["run_id"],),
        ).fetchall()
    assert streamed_workers == [("gw0",), ("gw1",)]
    assert 0 < payload["status"]["call_stream"]["value_bytes"] <= 7
    assert all(json.loads(row[0])["timing"]["complete"] for row in stream_payloads)
    assert all(json.loads(row[0])["executed_lines"] for row in stream_payloads)
    totals = {
        worker: (parent, json.loads(call)["frame_id"])
        for worker, parent, call in rows
        if json.loads(call)["name"] == "total"
    }
    assert set(totals) == {"gw0", "gw1"}
    assert all(parent is not None for parent, _ in totals.values())
    assert len({frame_id for _, frame_id in totals.values()}) == 2


def test_crashed_pytest_worker_prevents_saving_an_incomplete_baseline(tmp_path):
    pytest.importorskip("xdist")
    (tmp_path / "test_crash.py").write_text(
        "import os\n\ndef test_crash():\n    os._exit(7)\n"
    )
    result = invoke(
        tmp_path,
        "pytest",
        "test_crash.py",
        "--golden",
        "--",
        "-n",
        "1",
        "--max-worker-restart=0",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert report["status"]["missing_trace_workers"] == ["gw0"]
    assert report["snapshot"]["action"] == "unknown"
    assert not Path(report["snapshot"]["path"]).exists()


@pytest.mark.parametrize("max_trace_bytes", [1000, 100_000])
def test_late_unsampled_change_is_detected_at_and_below_storage_budget(
    tmp_path, max_trace_bytes
):
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.ldr.storage]\nbatch_size = 3\nmax_trace_bytes = {max_trace_bytes}\n"
    )
    program = tmp_path / "app.py"
    template = "def leaf(n):\n    return n + ({delta} if n == 37 else 0)\nfor i in range(50):\n    leaf(i)\n"
    program.write_text(template.format(delta=0))
    baseline = invoke(tmp_path, "run", "app.py", "--golden")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    program.write_text(template.format(delta=1))
    result = invoke(tmp_path, "run", "app.py", "--diff", "--fail-on-change")
    assert result.returncode == 3, result.stdout + result.stderr
    report = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert report["behavior"]["stream_changed_functions"] == ["app.py:leaf"]
    assert report["status"]["call_stream"]["started"] == 50
    stored = report["status"]["call_stream"]["stored"]
    assert stored == 50 if max_trace_bytes == 100_000 else 0 < stored < 50
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        (used,) = connection.execute(
            "SELECT used_bytes FROM stream_budget WHERE run_id = ?",
            (report["status"]["run_id"],),
        ).fetchone()
        assert used <= max_trace_bytes
        assert connection.execute(
            "SELECT count(*) FROM stream_calls WHERE run_id = ?",
            (report["status"]["run_id"],),
        ).fetchone() == (stored,)
