"""Hotfix A: no console window for any process LDR, its harness or its tests start.

Every process goes through ldr/processes.py, which adds CREATE_NO_WINDOW on Windows."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ldr import processes

REPO = Path(__file__).resolve().parents[1]
STARTERS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
OS_STARTERS = {"system", "popen", "startfile", "spawnl", "spawnv", "execv", "execl"}


def starts_a_process_directly(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "subprocess"
        for alias in node.names if alias.name in STARTERS
    }
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            if (function.value.id == "subprocess" and function.attr in STARTERS) or (
                function.value.id == "os" and function.attr in OS_STARTERS
            ):
                lines.append(node.lineno)
        elif isinstance(function, ast.Name) and function.id in imported:
            lines.append(node.lineno)
    return lines


def test_nothing_starts_a_process_except_through_the_helper():
    offenders = []
    for folder in ("ldr", "evaluation", "tests"):
        for path in sorted((REPO / folder).rglob("*.py")):
            if path.name in ("processes.py", "test_processes.py"):
                continue
            offenders += [f"{path.relative_to(REPO)}:{line}" for line in starts_a_process_directly(path)]
    assert offenders == []


def test_the_scan_sees_a_direct_call(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("import subprocess\nfrom subprocess import Popen as P\nsubprocess.run([])\nP([])\n", encoding="utf-8")
    assert starts_a_process_directly(sample) == [3, 4]


def test_the_helper_adds_no_window_on_windows_and_changes_nothing_elsewhere(monkeypatch):
    seen = []
    monkeypatch.setattr(processes.subprocess, "Popen", lambda command, **kwargs: seen.append(kwargs) or object())
    processes.Popen(["x"], creationflags=0x200)
    processes.Popen(["x"])
    if os.name == "nt":
        assert seen[0]["creationflags"] == 0x200 | 0x08000000 and seen[1]["creationflags"] == 0x08000000
    else:
        assert "creationflags" not in seen[0] and "creationflags" not in seen[1]


def test_the_helper_runs_a_process_and_returns_its_output():
    result = processes.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
    assert result.stdout.strip() == "hi"
    assert processes.check_output([sys.executable, "-c", "print(3)"], text=True).strip() == "3"
    assert processes.call([sys.executable, "-c", "pass"]) == 0 and processes.check_call([sys.executable, "-c", "pass"]) == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows creation flags")
def test_the_supervisor_has_no_window_a_new_process_group_and_breakaway(monkeypatch, tmp_path):
    from ldr import supervisor

    flags = []

    class Fake:
        pid = 0  # no such process: the supervisor's number is looked up and found missing
        stdin = type("S", (), {"write": lambda self, data: None, "close": lambda self: None})()

    monkeypatch.setattr(processes.subprocess, "Popen", lambda command, **kwargs: flags.append(kwargs["creationflags"]) or Fake())
    supervisor._start_real(str(tmp_path), b"{}")
    assert flags == [0x08000000 | 0x00000200 | 0x01000000]  # no window, new group, breakaway; not DETACHED_PROCESS (0x8)

    def refuse_breakaway(command, **kwargs):
        flags.append(kwargs["creationflags"])
        if kwargs["creationflags"] & 0x01000000:
            raise OSError("the job does not allow breaking away")
        return Fake()

    flags.clear()
    monkeypatch.setattr(processes.subprocess, "Popen", refuse_breakaway)
    supervisor._start_real(str(tmp_path), b"{}")
    assert flags == [0x08000000 | 0x00000200 | 0x01000000, 0x08000000 | 0x00000200]


def test_a_process_that_does_not_capture_its_output_still_writes_to_ours():
    # With CREATE_NO_WINDOW a child's console is its own: streams not given would be lost.
    code = (
        "import sys\n"
        "from ldr import processes\n"
        "processes.run([sys.executable, '-c', \"import sys; print('out-text'); print('err-text', file=sys.stderr)\"])\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, PYTHONPATH=str(REPO)))
    assert "out-text" in result.stdout and "err-text" in result.stderr, (result.stdout, result.stderr)
    child = subprocess.run([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], input="piped in", capture_output=True, text=True)
    assert child.stdout == "piped in"
