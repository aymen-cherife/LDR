"""--timeout must not silently do nothing."""

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))

@pytest.mark.skipif(hasattr(__import__("signal"), "SIGALRM"), reason="SIGALRM exists here")
def test_timeout_without_sigalrm_warns_instead_of_silently_doing_nothing(tmp_path):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\nf()\n")

    result = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", "ok.py", "--timeout", "5"],
        cwd=tmp_path, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    text = (tmp_path / ".ldr" / "last.txt").read_text()
    assert "--timeout is not enforced on this platform" in text
    assert "a hung run will not be stopped" in text.replace("\n", " ")

