"""Every process LDR, its harness and its tests start goes through here.

On Windows a process started from a program without a console opens a console window of its own
(a black box that flashes, or stays). CREATE_NO_WINDOW prevents that. Elsewhere nothing changes.
tests/test_processes.py fails if anything in ldr/, evaluation/ or tests/ starts a process any
other way.
"""

from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _options(kwargs: dict) -> dict:
    if CREATE_NO_WINDOW:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return kwargs


def Popen(command, **kwargs) -> subprocess.Popen:  # noqa: N802 (the name of what it replaces)
    return subprocess.Popen(command, **_options(kwargs))


def run(command, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, **_options(kwargs))


def call(command, **kwargs) -> int:
    return subprocess.call(command, **_options(kwargs))


def check_call(command, **kwargs) -> int:
    return subprocess.check_call(command, **_options(kwargs))


def check_output(command, **kwargs):
    return subprocess.check_output(command, **_options(kwargs))
