"""Defect 9: a run always says what it recorded, and warns when it recorded nothing."""

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))


def ldr(cwd, *args):
    return subprocess.run(
        [sys.executable, "-m", "ldr.cli", *args],
        cwd=cwd, env=ENV, capture_output=True, text=True,
    )


def last(tmp_path):
    return (
        (tmp_path / ".ldr" / "last.txt").read_text(),
        json.loads((tmp_path / ".ldr" / "last.json").read_text()),
    )


def test_every_run_prints_its_recorded_scope_and_call_count(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\nadd(1, 2)\n")

    ldr(tmp_path, "run", "app.py")

    text, payload = last(tmp_path)
    assert "SCOPE: " in text
    assert "1 project call(s) recorded" in text
    assert payload["scope"]["recorded"]
    assert payload["scope"]["project_calls"] == 1


def test_zero_project_calls_prints_a_warning_and_the_fix(tmp_path):
    (tmp_path / "noop.py").write_text("print('no functions here')\n")

    ldr(tmp_path, "run", "noop.py")

    text, payload = last(tmp_path)
    assert "0 project call(s) recorded" in text
    assert "No calls from project code were recorded" in text
    assert "--only" in text and "--project-root" in text
    assert payload["scope"]["project_calls"] == 0


def pytest_layout_without_git(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text("def total(xs):\n    return sum(xs)\n")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from pkg.calc import total\n\ndef test_t():\n    assert total([1, 2]) == 4\n"
    )


def test_pytest_without_git_warns_when_the_package_under_test_was_not_recorded(tmp_path):
    pytest_layout_without_git(tmp_path)

    ldr(tmp_path, "pytest", "tests/test_calc.py")

    text, _ = last(tmp_path)
    assert "0 project call(s) recorded" in text
    assert "No calls from project code were recorded" in text


@pytest.mark.xfail(
    strict=True,
    reason="defect 9 is open: a package without __init__.py is not recorded in pytest mode "
    "when there is no .git or pyproject.toml. Cause not found yet.",
)
def test_pytest_without_git_records_a_package_without_init(tmp_path):
    pytest_layout_without_git(tmp_path)

    ldr(tmp_path, "pytest", "tests/test_calc.py")

    text, _ = last(tmp_path)
    assert "total(xs=[1, 2])" in text
