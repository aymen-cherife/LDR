"""Controlled comparisons must restore the same prior recording for each commit."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
import final_check


def test_controlled_comparison_uses_commits_and_restores_recording(tmp_path, monkeypatch, capsys):
    # Own the history under test: CI intentionally has a shallow checkout.
    from ldr import processes
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(*args):
        return processes.check_output(["git", *args], cwd=repository, text=True).strip()
    git("init", "--quiet")
    git("-c", "user.name=Aymen Cherife", "-c",
        "user.email=153111060+aymen-cherife@users.noreply.github.com",
        "commit", "--quiet", "--allow-empty", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    git("-c", "user.name=Aymen Cherife", "-c",
        "user.email=153111060+aymen-cherife@users.noreply.github.com",
        "commit", "--quiet", "--allow-empty", "-m", "current")
    current = git("rev-parse", "HEAD")
    monkeypatch.setattr(final_check, "REPO", repository)
    root = tmp_path / "fixture"
    root.mkdir()
    calls = []

    def one(root, traced, env):
        directory = root / ".ldr"
        if not calls:
            directory.mkdir()
            (directory / "previous").write_text("reference")
        else:
            assert (directory / "previous").read_text() == "reference"
            (directory / "previous").write_text("changed by measured run")
        calls.append(env["checkout"])
        return {"post_run_seconds": {}}

    def clean(root):
        import shutil
        shutil.rmtree(root / ".ldr", ignore_errors=True)

    overhead = types.SimpleNamespace(
        build=lambda work, fixture: (root, None, [sys.executable, "-m", "ldr"]),
        bench_python=lambda work: sys.executable,
        clean=clean,
        environment=lambda checkout, root: {"checkout": checkout},
    )
    monkeypatch.setitem(sys.modules, "overhead", overhead)
    monkeypatch.setitem(sys.modules, "run_phases", types.SimpleNamespace(one=one, HASH_SEED="0"))
    final_check.controlled_comparison(baseline, tmp_path)
    assert len(calls) == 3
    assert calls[0] == calls[1]
    assert calls[1] != calls[2]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all('"pythonhashseed": "0"' in line for line in lines)
    import json
    rows = [json.loads(line) for line in lines]
    assert [row["commit"] for row in rows] == [baseline, current]
