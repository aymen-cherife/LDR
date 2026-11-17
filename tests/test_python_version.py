"""LDR refuses to start below Python 3.12, never silently."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldr import cli  # noqa: E402
from ldr.tracer import Recorder  # noqa: E402


def test_cli_refuses_python_311_with_a_clear_message(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "version_info", (3, 11, 9, "final", 0))

    code = cli.main(["run", "anything.py"])

    assert code == 2
    assert "LDR needs Python 3.12+. You are on 3.11." in capsys.readouterr().err
    assert not (tmp_path / ".ldr").exists()  # nothing ran, nothing was written


def test_recorder_refuses_when_sys_monitoring_is_missing(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 10, 4, "final", 0))
    monkeypatch.delattr(sys, "monitoring", raising=False)

    with pytest.raises(RuntimeError, match=r"LDR needs Python 3\.12\+\. You are on 3\.10\."):
        Recorder(scope=[os.getcwd()]).start()


def test_python_312_runs_normally(tmp_path, monkeypatch, capsys):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\nf()\n")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["run", "ok.py"]) == 0
