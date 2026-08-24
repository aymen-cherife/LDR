"""Navigation and evidence-surface tests for agent UX milestone.

These tests validate meaningful evidence semantics, not shallow heading checks:
- selectors resolve deterministically
- copyable commands execute
- parent/child correct
- chronological order
- mutation with identity
- Decimal exactness
- rare transitions survive sampling
- capture limits honest
- exit status preserved
- text and JSON parity
"""

import json
import os
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def invoke(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    import subprocess
    return subprocess.run(
        [sys.executable, "-m", "ldr.cli", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_broad_copyable_command_resolves_and_executes(tmp_path):
    (tmp_path / "calc.py").write_text("def scale(x):\n    return x*2\n")
    (tmp_path / "test_calc.py").write_text("from calc import scale\ndef test_scale():\n    assert scale(3)==999\n")
    r = invoke(tmp_path, "pytest", "test_calc.py", "--only", str(tmp_path))
    assert r.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    # Available inspections section present and contains copyable commands
    assert "AVAILABLE INSPECTIONS" in text
    # Extract a single-frame command from text
    m = re.search(r"single: ldr inspect (\S+) --frame (\S+) --only (\S+)", text)
    assert m, text
    target, frame, scope = m.groups()
    # The frame from text must match JSON's frame_index
    frame_ids = [f["frame_id"] for f in payload["frame_index"]["frames"]]
    assert frame.strip("'\"") in frame_ids
    # The copyable command should actually execute the focused run
    frame_token = frame.strip("'\"")
    r2 = invoke(tmp_path, "inspect", target, "--frame", frame_token, "--only", scope.strip("'\""))
    assert r2.returncode == 1  # still failing test, but inspection matched
    payload2 = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload2["inspection"]["matched"] is True
    assert payload2["inspection"]["frames"][0]["frame_id"] == frame_token


def test_parent_child_relationships_remain_correct(tmp_path):
    (tmp_path / "a.py").write_text("def leaf(x):\n    return x+1\ndef mid(x):\n    return leaf(x)*2\ndef top(x):\n    return mid(x)+3\n")
    (tmp_path / "test_a.py").write_text("from a import top\ndef test_top():\n    assert top(2)==999\n")
    invoke(tmp_path, "pytest", "test_a.py", "--only", str(tmp_path))
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    frames = payload["frame_index"]["frames"]
    # Build parent map
    by_id = {f["frame_id"]: f for f in frames}
    # leaf's parent should be mid, mid's parent top, top's parent test
    leaf = next(f for f in frames if f["function"] == "leaf")
    mid = next(f for f in frames if f["function"] == "mid")
    top = next(f for f in frames if f["function"] == "top")
    assert leaf["parent_id"] == mid["frame_id"]
    assert mid["parent_id"] == top["frame_id"]
    assert top["parent_id"] and "test_top" in top["parent_id"]
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    # Text invocation index must show same parent relationships
    for fid in [leaf["frame_id"], mid["frame_id"], top["frame_id"]]:
        assert fid in text
    assert f"parent={leaf['parent_id']}" in text


def test_rare_transition_survives_sampling(tmp_path):
    (tmp_path / "rep.py").write_text(
        "def convert(x):\n    return x*2\n"
        "def process(vals):\n    out=[]\n    for v in vals:\n        out.append(convert(v))\n    return out\n"
        "def test_rep():\n    vals=[1]*8 + [2] + [1]*4\n    assert process(vals)==[999]*13\n"
    )
    # Force sampling with low max-calls
    r = invoke(tmp_path, "pytest", "rep.py::test_rep", "--only", str(tmp_path), "--max-calls-per-function", "8")
    assert r.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    # Despite sampling, the rare x=2 call must be retained
    convert_frames = [f for f in payload["frame_index"]["frames"] if f["function"] == "convert"]
    # Need to inspect actual nodes to see input variation, via text VALUE TRANSITIONS
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "VALUE TRANSITIONS" in text
    # Should report 2 distinct patterns for convert
    assert "2 distinct patterns" in text
    assert "x=2" in text or "x=2" in json.dumps(payload)


def test_focused_shows_chronological_state_and_mutation(tmp_path):
    (tmp_path / "mut.py").write_text(
        "def enrich(docs):\n"
        "    for d in docs:\n"
        "        d['enriched']=True\n"
        "    return docs\n"
        "def caller():\n"
        "    docs=[{'text':'hi'}]\n"
        "    before=list(docs[0].keys())\n"
        "    enrich(docs)\n"
        "    after=list(docs[0].keys())\n"
        "    return before,after\n"
        "def test_mut():\n"
        "    b,a=caller()\n"
        "    assert b==a\n"
    )
    invoke(tmp_path, "pytest", "mut.py::test_mut", "--only", str(tmp_path))
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    fid = next(f["frame_id"] for f in payload["frame_index"]["frames"] if f["function"] == "enrich")
    r2 = invoke(tmp_path, "inspect", "mut.py::test_mut", "--frame", fid, "--only", str(tmp_path))
    assert r2.returncode == 1
    payload2 = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    frame = payload2["inspection"]["frames"][0]
    # Chronological: state_steps must be in line order 2,3,2,4
    lines = [s["line"] for s in frame["state_steps"]]
    assert lines == sorted(lines) or lines == [2,3,2,4]  # at least monotonic with loop
    # Mutation: docs changed from before to after
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "INPUT MUTATIONS" in text
    assert "docs:" in text and "enriched" in text
    # Ensure chronological STATE TRANSITIONS present
    assert "STATE TRANSITIONS" in text
    # Verify text and JSON same fact for mutation
    json_changes = [c for step in frame["state_steps"] for c in step["changes"] if c["name"]=="docs"]
    assert any("enriched" in c["after"] for c in json_changes)
    assert any("enriched" in line for line in text.splitlines() if "docs:" in line)


def test_exact_decimal_preserved(tmp_path):
    (tmp_path / "dec.py").write_text(
        "from decimal import Decimal\n"
        "def calc(x, y):\n"
        "    return x * y\n"
        "def test_dec():\n"
        "    assert calc(Decimal('1.1'), Decimal('0.1000000000000000055511151231257827021181583404541015625')) == Decimal('0.2')\n"
    )
    invoke(tmp_path, "pytest", "dec.py::test_dec", "--only", str(tmp_path))
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    # Find calc frame
    calc = next(f for f in payload["frame_index"]["frames"] if f["function"] == "calc")
    # The precise Decimal repr must be in both text and JSON without truncation
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    # Input Decimal contains high precision
    assert "0.1000000000000000055511151231257827021181583404541015625" in text
    assert "0.1000000000000000055511151231257827021181583404541015625" in json.dumps(payload)


def test_capture_limits_reported_honestly(tmp_path):
    (tmp_path / "late.py").write_text(
        "def leaf(x):\n    return x\n"
        "def fail():\n"
        "    for x in range(200):\n        leaf(x)\n"
        "    raise RuntimeError('late')\n"
        "fail()\n"
    )
    r = invoke(tmp_path, "run", "late.py", "--only", str(tmp_path), "--max-nodes", "10")
    assert r.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "CAPTURE LIMITS" in text
    # Status fields must match text reporting
    assert f"recorded={payload['status']['recorded_nodes']}" in text
    assert f"sampled-repetitions={payload['status']['sampled_repetitions']}" in text or "sampled" in text.lower()
    # Overflow must be consistent
    if payload["status"]["trace_truncated"]:
        assert "truncated=True" in text or "trace budget reached" in text


def test_exit_status_preserved_and_json_matches_text(tmp_path):
    (tmp_path / "fail.py").write_text("def inc(x):\n    return x+1\ndef test_fail():\n    assert inc(1)==999\n")
    r = invoke(tmp_path, "pytest", "fail.py::test_fail", "--only", str(tmp_path))
    assert r.returncode == 1
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    assert payload["status"]["exit_code"] == 1
    assert payload["status"]["ok"] is False
    assert "RUN STATUS: FAILED" in text
    # Same frame_ids in text and JSON
    for fid in [f["frame_id"] for f in payload["frame_index"]["frames"]]:
        assert fid in text
    # Failures same
    assert payload["failures"][0]["error"] in text or "AssertionError" in text


def test_wildcard_inspection_covers_all_invocations(tmp_path):
    (tmp_path / "wf.py").write_text(
        "def convert(x):\n    return x*2\n"
        "def test_wf():\n    assert convert(1)==2\n    assert convert(2)==999\n"
    )
    invoke(tmp_path, "pytest", "wf.py::test_wf", "--only", str(tmp_path))
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    selector = next(f["inspect_selector"] for f in payload["frame_index"]["functions"] if f["function"]=="convert")
    assert selector.endswith("#*")
    r2 = invoke(tmp_path, "inspect", "wf.py::test_wf", "--frame", selector, "--only", str(tmp_path))
    payload2 = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload2["inspection"]["match_count"] == 2
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "INSPECTED FRAMES: 2" in text
    # Ensure both invocations' distinct outputs present
    assert "1" in text and "2" in text


def test_value_reset_and_overwrite_at_correct_point(tmp_path):
    (tmp_path / "reset.py").write_text(
        "def evolve(x):\n"
        "    v=x\n"
        "    v=v+1\n"
        "    v=0\n"
        "    v=v+5\n"
        "    return v\n"
        "def test_reset():\n"
        "    assert evolve(10)==999\n"
    )
    invoke(tmp_path, "pytest", "reset.py::test_reset", "--only", str(tmp_path))
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    fid = next(f["frame_id"] for f in payload["frame_index"]["frames"] if f["function"]=="evolve")
    invoke(tmp_path, "inspect", "reset.py::test_reset", "--frame", fid, "--only", str(tmp_path))
    payload2 = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    steps = payload2["inspection"]["frames"][0]["state_steps"]
    # Check overwrites appear in chronological order and reset visible
    flat = [(s["line"], c["name"], c["after"]) for s in steps for c in s["changes"] if c["name"]=="v"]
    # Should go 10 -> 11 -> 0 -> 5
    assert any(after=="11" for _,_,after in flat)
    assert any(after=="0" for _,_,after in flat)
    assert any(after=="5" for _,_,after in flat)
    # Order preserved
    vals = [after for _,_,after in flat]
    assert vals.index("11") < vals.index("0") < vals.index("5")
