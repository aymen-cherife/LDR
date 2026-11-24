"""One version, everywhere: schema 7 broke old snapshots, so this is 0.6.0."""

import os
import re
import subprocess
import sys

import ldr
from ldr import processes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_version_is_0_6_0_and_consistent():
    pyproject = re.search(r'^version = "([^"]+)"', read("pyproject.toml"), re.M).group(1)
    assert pyproject == ldr.__version__ == "0.6.0"
    shown = processes.run(
        [sys.executable, "-m", "ldr", "--version"],
        env=dict(os.environ, PYTHONPATH=ROOT), capture_output=True, text=True,
    ).stdout.strip()
    assert shown == "ldr 0.6.0"
    assert "LDR 0.6.0" in read("README.md")


def test_changelog_says_old_snapshots_must_be_rerecorded():
    top = read("CHANGELOG.md").split("## 0.5.2")[0]
    assert top.lstrip().startswith("# Changelog")
    assert "## 0.6.0" in top
    assert "schema 7" in top and "re-record" in top.lower()
