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


def test_a_field_that_varied_is_still_compared_and_the_change_carries_a_note(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT)
    assert ldr(tmp_path, "--golden", "--learn-volatility").returncode == 0

    result = ldr(tmp_path, "--diff")

    # The random value changed between the snapshot and this run: it is a change, counted
    # and shown, and the note says it also changed between two runs of identical code.
    assert "No behavioural change" not in result.stdout
    assert "pick" in result.stdout.split("BEST GUESS")[1]
    assert "note: out also varied across 2 runs with no edit" in result.stdout
    assert "stable(" not in result.stdout.split("BEST GUESS")[1].split("VARIED")[0]
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["behavior"]["changed"] is True
    assert payload["behavior"]["finding_count"] >= 1
    assert payload["behavior"]["unstable_fields"][0]["field"] == "out"


def test_saving_a_baseline_lists_what_varied_between_the_two_runs(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT)

    result = ldr(tmp_path, "--golden", "--learn-volatility")

    assert "VARIED BETWEEN IDENTICAL RUNS" in result.stdout
    assert "pick" in result.stdout.split("VARIED BETWEEN")[1]


def test_a_difference_beyond_the_preview_is_found_and_carries_the_note(tmp_path):
    """A random list differing only past the 6-item preview must still be learned."""
    (tmp_path / "app.py").write_text(
        "import random\n"
        "def noisy():\n"
        "    return [0, 1, 2, 3, 4, 5] + [random.random()]\n"
        "noisy()\n"
    )
    ldr(tmp_path, "--golden", "--learn-volatility")

    result = ldr(tmp_path, "--diff")

    assert "noisy" in result.stdout
    assert "also varied across 2 runs with no edit" in result.stdout
