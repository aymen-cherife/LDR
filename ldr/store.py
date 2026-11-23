"""Atomic, invocation-scoped persistence for traces and agent results."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any

from .redact import redact_argv

SCHEMA_VERSION = 7


def _portable_path(value: str, root: str) -> str:
    absolute = os.path.realpath(value)
    try:
        if os.path.commonpath((absolute, root)) == root:
            return os.path.relpath(absolute, root).replace(os.sep, "/")
    except ValueError:
        pass
    return absolute


def _portable_argument(value: str, root: str) -> str:
    prefix = ""
    candidate = value
    if "=" in value and value.startswith("-"):
        prefix, candidate = value.split("=", 1)
        prefix += "="
    file_part = candidate.split("::", 1)[0]
    suffix = candidate[len(file_part):]
    if file_part and os.path.exists(file_part):
        return prefix + _portable_path(file_part, root) + suffix
    return value


def invocation_descriptor(
    runner: str,
    target: str,
    arguments: list[str],
    scope: list[str],
    cwd: str | None = None,
    project_root: str | None = None,
    only: list[str] | None = None,
) -> dict[str, Any]:
    cwd = os.path.realpath(cwd or os.getcwd())
    project_root = os.path.realpath(project_root or cwd)
    target_file = target.split("::", 1)[0]
    suffix = target[len(target_file):]
    normalized_target = target
    if os.path.exists(target_file):
        normalized_target = _portable_path(target_file, project_root) + suffix
    return {
        "runner": runner,
        "target": normalized_target,
        # Credentials on the command line must not be stored or hashed into a key.
        "arguments": [
            _portable_argument(argument, project_root) for argument in redact_argv(arguments)
        ],
        "scope": sorted({_portable_path(path, project_root) for path in scope}),
        # What the user asked to trace with --only. The scope LDR infers is not part of the command.
        "only": sorted({_portable_path(path, project_root) for path in only or ()}),
    }


def invocation_key(descriptor: dict[str, Any]) -> str:
    # The command: runner, target, arguments and --only. The inferred scope is left out, so
    # adding a package to the project does not make the same command a different target.
    command = {key: descriptor.get(key) for key in ("runner", "target", "arguments")}
    command["only"] = descriptor.get("only") or []
    payload = json.dumps(command, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def atomic_json(path: str, payload: Any) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ldr-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_text(path: str, text: str) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ldr-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
