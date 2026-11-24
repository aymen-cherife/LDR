"""`python -m ldr` works."""

import os
import subprocess
import sys
from ldr import processes


HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))

def test_python_dash_m_ldr_works(tmp_path):
    """`ldr.exe` can be blocked (Windows application control); `python -m ldr` is the fallback."""
    (tmp_path / "ok.py").write_text("def f():\n    return 1\nf()\n")

    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "ok.py"],
        cwd=tmp_path, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "f() -> 1" in result.stdout
    assert processes.run(
        [sys.executable, "-m", "ldr", "--version"], env=ENV, capture_output=True, text=True,
    ).stdout.startswith("ldr ")

