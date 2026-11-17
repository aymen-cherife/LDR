"""Defect 12: fields that differ between two identical runs are shown as unstable,
never silently masked."""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))

SCRIPT = (
    "import random\n"
    "def pick():\n"
    "    return random.random()\n"
    "def stable(x):\n"
    "    return x + 1\n"
    "pick()\n"
    "stable(1)\n"
)


def ldr(cwd, *args):
    return subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", "app.py", *args],
        cwd=cwd, env=ENV, capture_output=True, text=True,
    )


def test_masked_fields_are_listed_when_comparing(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT)
    assert ldr(tmp_path, "--golden", "--learn-volatility").returncode == 0

    result = ldr(tmp_path, "--diff")

    assert "No behavioural change" in result.stdout  # the unstable field is not a finding...
    assert "UNSTABLE (shown, not compared)" in result.stdout  # ...but it is never hidden
    assert "pick" in result.stdout.split("UNSTABLE")[1]
    assert "out" in result.stdout.split("UNSTABLE")[1]
    assert "stable(" not in result.stdout.split("UNSTABLE")[1]
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["behavior"]["unstable_fields"][0]["field"] == "out"


def test_saving_a_baseline_lists_what_was_found_unstable(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT)

    result = ldr(tmp_path, "--golden", "--learn-volatility")

    assert "UNSTABLE (shown, not compared)" in result.stdout
    assert "pick" in result.stdout.split("UNSTABLE")[1]


def test_unstable_beyond_the_preview_is_found_too(tmp_path):
    """A random list differing only past the 6-item preview must still be learned."""
    (tmp_path / "app.py").write_text(
        "import random\n"
        "def noisy():\n"
        "    return [0, 1, 2, 3, 4, 5] + [random.random()]\n"
        "noisy()\n"
    )
    ldr(tmp_path, "--golden", "--learn-volatility")

    result = ldr(tmp_path, "--diff")

    assert "No behavioural change" in result.stdout
    assert "noisy" in result.stdout.split("UNSTABLE")[1]
