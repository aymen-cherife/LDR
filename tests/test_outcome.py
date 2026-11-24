"""Defect 8: every run records and shows its outcome and commit, as fact."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from ldr import processes

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def ldr(cwd, *args):
    return processes.run(
        [sys.executable, "-m", "ldr.cli", *args],
        cwd=cwd, env=ENV, capture_output=True, text=True,
    )


def git(cwd, *args):
    return processes.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                 GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"),
    ).stdout.strip()


def make_repo(path, body):
    (path / "m.py").write_text(body)
    git(path, "init", "-q")
    git(path, "add", "m.py")
    git(path, "commit", "-q", "-m", "init")
    return git(path, "rev-parse", "HEAD")


FAILING = "def top(x):\n    return x + 1\ntop(1)\nraise SystemExit(1)\n"
PASSING = "def top(x):\n    return x + 1\ntop(1)\n"


@needs_git
def test_a_failing_run_states_its_outcome_and_commit(tmp_path):
    head = make_repo(tmp_path, FAILING)

    result = ldr(tmp_path, "run", "m.py")

    assert "RUN STATUS: FAILED (exit 1)" in result.stdout and f"COMMIT: {head[:7]}" in result.stdout
    status = json.loads((tmp_path / ".ldr" / "last.json").read_text())["status"]
    assert status["commit"] == {"id": head, "dirty": False}
    assert status["exit_code"] == 1


@needs_git
def test_uncommitted_changes_are_stated_not_judged(tmp_path):
    head = make_repo(tmp_path, PASSING)
    (tmp_path / "m.py").write_text(PASSING + "\n# edited\n")

    result = ldr(tmp_path, "run", "m.py")

    assert f"COMMIT: {head[:7]} (uncommitted changes)" in result.stdout
    assert json.loads((tmp_path / ".ldr" / "last.json").read_text())["status"]["commit"]["dirty"] is True


@needs_git
def test_the_comparison_names_the_outcome_and_commit_of_both_runs(tmp_path):
    first = make_repo(tmp_path, PASSING)
    ldr(tmp_path, "run", "m.py")
    (tmp_path / "m.py").write_text(FAILING)
    git(tmp_path, "commit", "-q", "-am", "break it")
    second = git(tmp_path, "rev-parse", "HEAD")

    result = ldr(tmp_path, "run", "m.py")

    assert (
        f"PREVIOUS RUN: outcome PASSED (exit 0), commit {first[:7]}. "
        f"THIS RUN: outcome FAILED (exit 1), commit {second[:7]}."
    ) in result.stdout


def test_without_git_the_commit_is_reported_as_none(tmp_path):
    (tmp_path / "m.py").write_text(PASSING)

    result = ldr(tmp_path, "run", "m.py")

    assert "COMMIT: none (no git repository found)" in result.stdout
    assert json.loads((tmp_path / ".ldr" / "last.json").read_text())["status"]["commit"] is None


def test_ldr_never_writes_to_git(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    make_repo(tmp_path, PASSING)
    before = {
        str(p.relative_to(tmp_path)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in (tmp_path / ".git").rglob("*") if p.is_file()
    }

    ldr(tmp_path, "run", "m.py")
    ldr(tmp_path, "run", "m.py")

    after = {
        str(p.relative_to(tmp_path)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in (tmp_path / ".git").rglob("*") if p.is_file()
    }
    assert after == before

@pytest.mark.parametrize(("value", "code"), [("None", 0), ("7", 7), ("[]", 1), ("''", 1)])
def test_system_exit_uses_python_exit_codes(tmp_path, value, code):
    (tmp_path / "m.py").write_text(f"raise SystemExit({value})\n")
    for flags in ((), ("--untraced-time",)):
        assert ldr(tmp_path, "run", "m.py", *flags).returncode == code
