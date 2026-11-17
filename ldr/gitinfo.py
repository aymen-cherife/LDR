"""Read-only git facts. LDR reads git and never writes to it.

``GIT_OPTIONAL_LOCKS=0`` stops ``git status`` from refreshing the index, so the
user's repository is byte-for-byte unchanged after an LDR run.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

_TIMEOUT_SECONDS = 5


def _git(root: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git_state(root: str) -> dict[str, Any] | None:
    """``{"id": <full sha>, "dirty": bool}`` for the repository holding ``root``, else None.

    ``dirty`` means tracked files differ from HEAD; untracked files are not counted.
    """
    head = _git(root, "rev-parse", "HEAD")
    if head is None or head.returncode != 0 or not head.stdout.strip():
        return None
    changes = _git(root, "status", "--porcelain", "--untracked-files=no")
    dirty = bool(changes is not None and changes.returncode == 0 and changes.stdout.strip())
    return {"id": head.stdout.strip(), "dirty": dirty}


def describe_commit(commit: dict[str, Any] | None) -> str:
    if not commit:
        return "none (no git repository found)"
    text = str(commit.get("id", ""))[:7]
    return f"{text} (uncommitted changes)" if commit.get("dirty") else text


def describe_outcome(status: dict[str, Any]) -> str:
    code = status.get("exit_code")
    if status.get("crashed"):
        return f"CRASHED (exit {code})"
    return f"{'PASSED' if code == 0 else 'FAILED'} (exit {code})"
