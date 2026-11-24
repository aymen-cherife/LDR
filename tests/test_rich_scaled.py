"""Batch 2, step 1: the scaled copy of the rich benchmark (evaluation/rich_scaled.py).

The fixture has the object types, sizes, roles and call counts of the rich run at 1/100, and
repeats objects (the same ThemeStack is returned again and again) the way rich does."""

import collections
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evaluation"))

import rich_scaled  # noqa: E402

from ldr import fingerprint  # noqa: E402

SPEC = rich_scaled.load()


def distinct_slots(limit=250):
    """(type name, nodes) of values the fixture builds, the largest and a spread of the rest."""
    seen = {}
    for call in SPEC["calls"]:
        for slot in [*call["a"], *([[0, *call["r"]]] if call.get("r") else [])]:
            _, type_index, nodes, _, _, grown = slot
            for size in (nodes, grown):
                if size > 1:
                    seen[(SPEC["type_names"][type_index], size)] = None
    ordered = sorted(seen, key=lambda item: -item[1])
    return ordered[:25] + ordered[25::max(1, len(ordered) // limit)]


def test_a_built_value_has_the_nodes_the_walk_of_the_fingerprint_reads():
    for name, nodes in distinct_slots():
        value, _ = rich_scaled.build(name, nodes, SPEC["types"].get(name))
        before = fingerprint.STATS.walks
        fingerprint.fingerprint_details(value)
        assert fingerprint.STATS.walks == before + 1, name
        assert fingerprint.STATS.walked == nodes, (name, nodes, fingerprint.STATS.walked)
        assert rich_scaled.nodes_of(value) == nodes, name


def test_the_largest_value_is_a_theme_stack_of_2847_nodes():
    sizes = {nodes for name, nodes in distinct_slots() if name == "ThemeStack"}
    assert 2847 in sizes
    value, _ = rich_scaled.build("ThemeStack", 2847, SPEC["types"]["ThemeStack"])
    assert type(value).__name__ == "ThemeStack" and rich_scaled.nodes_of(value) == 2847


def test_the_fixture_is_deterministic(tmp_path):
    first = rich_scaled.generate(tmp_path / "one").read_text(encoding="utf-8")
    second = rich_scaled.generate(tmp_path / "two").read_text(encoding="utf-8")
    assert first == second
    sizes = [
        [(f, [rich_scaled.nodes_of(a) for a in arguments], rich_scaled.nodes_of(returned), len(bumps), len(growth))
         for f, arguments, returned, bumps, growth in rich_scaled.prepare(SPEC)]
        for _ in range(2)
    ]
    assert sizes[0] == sizes[1]


def test_the_same_theme_stack_is_returned_again_and_again():
    returned = [step[2] for step in rich_scaled.prepare(SPEC) if rich_scaled.nodes_of(step[2]) == 2847]
    assert len(returned) >= 10
    assert len({id(value) for value in returned}) < len(returned) // 2


def test_the_spec_has_a_hundredth_of_the_calls():
    calls = collections.Counter(call["f"] for call in SPEC["calls"])
    assert sum(calls.values()) == len(SPEC["calls"])
    assert len(SPEC["calls"]) == pytest.approx(SPEC["calls_in_source"] / SPEC["divisor"], abs=1)
    assert set(calls) <= set(range(len(SPEC["functions"])))


def test_the_recorded_run_has_the_calls_sizes_and_roles_of_the_spec(tmp_path):
    root = rich_scaled.generate(tmp_path / "fixture").parent
    result = subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py"], cwd=root, capture_output=True, text=True, timeout=900,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO), str(REPO / "evaluation")])),
    )
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-1500:]
    assert float((root / "loop_seconds.txt").read_text()) > 0
    with closing(sqlite3.connect(root / ".ldr" / "traces.sqlite3")) as connection:
        run = connection.execute("SELECT id FROM runs").fetchone()[0]
        calls = connection.execute("SELECT COUNT(*) FROM call_records WHERE run_id = ?", (run,)).fetchone()[0]
        rows = connection.execute(
            "SELECT role, type, nodes FROM value_sizes WHERE run_id = ?", (run,)
        ).fetchall()
    source, divisor = SPEC["source"], SPEC["divisor"]
    assert calls == len(SPEC["calls"])
    total = sum(nodes for _, _, nodes in rows)
    assert total == pytest.approx(source["nodes"] / divisor, rel=0.05)
    assert len(rows) == pytest.approx(source["walks"] / divisor, rel=0.05)
    for role, share in source["role_share"].items():
        found = sum(nodes for r, _, nodes in rows if r == role) / total
        assert found == pytest.approx(share, abs=0.04), role
    stacks = [nodes for _, kind, nodes in rows if kind == "ThemeStack"]
    assert max(stacks) == 2847
    big = sum(nodes for _, kind, nodes in rows if kind in ("ThemeStack", "Theme")) / total
    assert big == pytest.approx(source["type_share"]["ThemeStack"] + source["type_share"]["Theme"], abs=0.06)
