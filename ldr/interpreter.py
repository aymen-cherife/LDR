"""Choose the interpreter that runs the target, and start LDR in it.

The target runs in a child process, in the project's interpreter, so that the
code under test sees its own packages and so that LDR controls how that
interpreter starts (for example its hash seed).
"""

from __future__ import annotations

import os
import subprocess
import sys

CHILD_MARKER = "LDR_RUN_CHILD"
SELECTED_BY = "LDR_INTERPRETER"
RUNNING = "running interpreter"


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def _python_in(environment_root: str) -> str | None:
    names = (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3"))
    for folder, name in names:
        candidate = os.path.join(environment_root, folder, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def choose(project_root: str, environ=None) -> tuple[str, str]:
    """Return ``(interpreter path, how it was chosen)``.

    1. The interpreter running ``ldr`` when it belongs to the project: its
       environment is inside the project or is the active ``VIRTUAL_ENV``.
    2. The active ``VIRTUAL_ENV``.
    3. The project's ``.venv``.
    4. Otherwise the interpreter running ``ldr``, because nothing else was found.
    """
    environ = os.environ if environ is None else environ
    active = environ.get("VIRTUAL_ENV")
    if _inside(sys.prefix, project_root) or (
        active and os.path.realpath(sys.prefix) == os.path.realpath(active)
    ):
        return sys.executable, RUNNING
    if active:
        python = _python_in(active)
        if python:
            return python, "VIRTUAL_ENV"
    python = _python_in(os.path.join(project_root, ".venv"))
    if python:
        return python, ".venv"
    return sys.executable, f"{RUNNING}, no project environment found"


def _same(first: str, second: str) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def importable(python: str, environ) -> bool:
    if _same(python, sys.executable):
        return True
    try:
        probe = subprocess.run(
            [python, "-c", "import ldr"], env=environ, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def run_in_child(argv: list[str], project_root: str) -> int:
    """Run ``ldr argv`` in the project's interpreter and return its exit status."""
    python, how = choose(project_root)
    environ = dict(os.environ)
    if not importable(python, environ):
        print(
            f"LDR is not installed in {python} ({how}). Install LDR in that environment, "
            "or run ldr from it.",
            file=sys.stderr,
        )
        return 2
    from .sink import ENV as SINK_ENV, Sink

    # The child sends its records here and this process writes them.
    sink = Sink()
    environ[CHILD_MARKER] = "1"
    environ[SELECTED_BY] = how
    environ[SINK_ENV] = sink.token
    try:
        process = subprocess.Popen([python, "-m", "ldr.cli", *argv], env=environ)
    except OSError as exc:
        print(f"LDR could not start {python}: {exc}", file=sys.stderr)
        sink.close(2)
        return 2
    code = _wait(process)
    message = sink.close(code)
    if message:
        print(message, file=sys.stderr)
    return code


def _wait(process) -> int:
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The child received the same interrupt. Give it time to save its result.
            try:
                return process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait()
            except KeyboardInterrupt:
                process.kill()
                return process.wait()
