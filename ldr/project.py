"""Where a run's project is, found without importing the rest of LDR.

The launcher needs this to choose the project's interpreter before anything else loads.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile

KNOWN_RUNNERS = {"unittest", "pytest", "doctest", "pdb", "cProfile", "trace", "timeit"}
PROJECT_MARKERS = (".git", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini")


def target_origin(mode: str, target: str, rest: list[str]) -> str | None:
    if mode in ("run", "pytest"):
        file_part = target.split("::", 1)[0]
        return os.path.realpath(file_part) if os.path.exists(file_part) else None
    module_target = target
    if target in KNOWN_RUNNERS:
        module_target = next((item for item in rest if item and not item.startswith("-")), "")
    if not module_target:
        return None
    candidate = module_target.split("::", 1)[0]
    while candidate:
        try:
            spec = importlib.util.find_spec(candidate)
        except (AttributeError, ImportError, ModuleNotFoundError, ValueError):
            spec = None
        if spec and spec.origin:
            return os.path.realpath(spec.origin)
        candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
    return None


def infer_project_root(mode: str, target: str, rest: list[str], cwd: str) -> str:
    origin = target_origin(mode, target, rest)
    start = os.path.dirname(origin) if origin and os.path.isfile(origin) else (origin or cwd)
    fallback = current = os.path.realpath(start)
    generic_roots = {
        os.path.realpath(os.path.abspath(os.sep)),
        os.path.realpath(tempfile.gettempdir()),
    }
    while True:
        if current not in generic_roots and any(
            os.path.exists(os.path.join(current, marker)) for marker in PROJECT_MARKERS
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return fallback
        if (
            mode == "pytest"
            and parent == cwd
            and cwd not in generic_roots
            and os.path.basename(current).lower() in {"test", "tests"}
        ):
            # A markerless project may keep its source beside the test directory.
            fallback = cwd
        current = parent
