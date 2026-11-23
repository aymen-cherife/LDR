"""The ``ldr`` command's entry point. It loads as little as it can.

A recording run (``run``, ``pytest`` or ``module`` with a target) needs only the project's
interpreter and the sink that writes the records, so the child that runs the program is
started before the rest of LDR is imported. Everything else (``show``, ``--detail``,
``verify``, ``inspect``, help and every argument error) goes to the full command line.
"""

from __future__ import annotations

import os
import sys

_FULL_PATH_FLAGS = {"-h", "--help", "--version", "--detail", "--frame-id"}


def _light_run(argv: list[str]):
    """(mode, project root) when this is a plain recording run, else None."""
    if len(argv) < 2 or argv[0] not in ("run", "pytest", "module") or argv[1].startswith("-"):
        return None
    before = argv[: argv.index("--")] if "--" in argv else argv
    from .project import KNOWN_RUNNERS

    if argv[0] == "module" and argv[1] in KNOWN_RUNNERS:
        return None  # the real target is the first plain word after it: the full parser reads that
    if _FULL_PATH_FLAGS & set(before) or os.environ.get("LDR_RUN_CHILD"):
        return None
    cwd = os.path.realpath(os.getcwd())
    root = None
    for index, item in enumerate(before):
        if item == "--project-root" and index + 1 < len(before):
            root = before[index + 1]
        elif item.startswith("--project-root="):
            root = item.split("=", 1)[1]
    if root is not None:
        root = os.path.realpath(root)
        return (argv[0], root) if os.path.isdir(root) else None
    from .project import infer_project_root

    return argv[0], infer_project_root(argv[0], argv[1], argv[2:], cwd)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 12):
        from .cli import main as full  # prints the version message

        return full(argv)
    light = _light_run(argv)
    if light is None:
        from .cli import main as full

        return full(argv)
    from .redact import register_argv_secrets

    register_argv_secrets(argv)
    from .interpreter import run_in_child

    return run_in_child(argv, light[1])


if __name__ == "__main__":
    raise SystemExit(main())
