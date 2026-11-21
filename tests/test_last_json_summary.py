"""Speed step 5: last.json is a summary under 30,000 characters; the rest stays in SQLite."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from ldr.summary import LIMIT, SHOWN, summarize

REPO = Path(__file__).resolve().parents[1]


def ldr(project, *args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args],
        cwd=project, env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def many_functions(count):
    lines = [f"def step_{n}(value):\n    return value + {n}\n" for n in range(count)]
    lines.append("total = 0\n")
    lines.extend(f"total = step_{n}(total)\n" for n in range(count))
    return "\n".join(lines)


def test_a_large_result_is_cut_to_a_summary_and_kept_whole_in_sqlite(tmp_path):
    (tmp_path / "app.py").write_text(many_functions(300), encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    text = (tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8")
    assert len(text) < LIMIT
    result = json.loads(text)
    index = result["frame_index"]
    assert index["total"] == 300 and len(index["frames"]) == SHOWN
    cut = {item["part"]: item for item in result["summary"]["cut"]}
    assert cut["frame_index.frames"] == {"part": "frame_index.frames", "shown": SHOWN, "total": 300}
    whole = result["summary"]["whole_in"]
    assert whole["stored"] is True and whole["table"] == "run_sections"
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        saved = connection.execute(
            "SELECT payload FROM run_sections WHERE run_id = ? AND name = 'frame_index'",
            (whole["run_id"],),
        ).fetchone()
    assert len(json.loads(saved[0])["frames"]) == 300


def test_a_small_result_is_unchanged(tmp_path):
    (tmp_path / "app.py").write_text(many_functions(5), encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    result = json.loads((tmp_path / ".ldr" / "last.json").read_text(encoding="utf-8"))
    assert "summary" not in result
    assert len(result["frame_index"]["frames"]) == 5
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_sections").fetchone() == (0,)


def test_a_frame_beyond_the_summary_can_still_be_inspected_by_id(tmp_path):
    (tmp_path / "app.py").write_text(many_functions(300), encoding="utf-8")
    ldr(tmp_path, "run", "app.py")
    result = ldr(tmp_path, "inspect", "--frame-id", "200")
    assert "step_199" in result.stdout


def test_the_guard_halves_the_largest_list_until_it_fits_and_says_so():
    findings = [{"function": f"f{n}", "value_changes": ["x" * 400] * 3} for n in range(40)]
    payload = {"status": {"run_id": "r1"}, "findings": findings, "failures": [], "suggestions": []}
    short, sections = summarize(payload)
    assert len(json.dumps(short)) < LIMIT
    (cut,) = short["summary"]["cut"]
    assert cut["part"] == "findings" and cut["total"] == 40 and cut["shown"] < SHOWN
    assert sections == {"findings": findings}
    assert payload["findings"] is findings and len(findings) == 40  # the input is not changed
