"""Agent-first command line interface for recording and behavioural diffing."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import runpy
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any

from . import __version__
from . import environment as run_environment
from .interpreter import CHILD_MARKER, RUNNING, SELECTED_BY, run_in_child
from .diff import (
    EQUAL_READABLE,
    align,
    compare_fields,
    diff_tree,
    mark_unstable,
    rank,
    render_unstable,
    unstable_fields,
)
from .gitinfo import describe_commit, describe_outcome, git_state
from .node import Node, capture, capture_error, count_redacted
from .redact import redact, redact_argv, register_argv_secrets
from .render import render_call_chain, render_diff, render_inspection, render_run
from .store import (
    SCHEMA_VERSION,
    atomic_json,
    atomic_text,
    invocation_descriptor,
    invocation_key,
    load_trace,
    save_trace,
    snapshot_path,
)
from .suggest import rank_inspections
from .call_stream import CallStream, changed_functions
from .sqlite_store import TraceStore, storage_settings
from .tracer import Recorder, python_version_message
from .value_capture import first_difference

LDR_DIR = ".ldr"
_KNOWN_RUNNERS = {"unittest", "pytest", "doctest", "pdb", "cProfile", "trace", "timeit"}
_PROJECT_MARKERS = (".git", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini")
_PACKAGE_ROOT_EXCLUDES = {
    ".git", ".github", ".ldr", ".tox", ".venv", "build", "dist", "docs",
    "documentation", "examples", "scripts", "test", "tests", "tools", "venv",
}


class _RunTimedOut(BaseException):
    pass


def _record(
    fn,
    scope: list[str],
    *,
    timeout: float = 300,
    defer_start: bool = False,
    call_stream: CallStream | None = None,
    **kwargs,
):
    recorder = Recorder(scope=scope, **kwargs)
    recorder.call_stream = call_stream
    recorder.crashed = False
    recorder.exit_code = 0
    recorder.crash_summary = ""
    recorder.escaped_exception = None
    recorder.runner_exception = None
    previous_handler = None
    alarm_enabled = timeout > 0 and hasattr(signal, "SIGALRM")

    def expired(signum, frame):
        raise _RunTimedOut(f"execution exceeded {timeout:g} seconds")

    if not defer_start:
        recorder.start()
    try:
        if alarm_enabled:
            previous_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            fn(recorder)
        except SystemExit as exc:
            recorder.exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        except _RunTimedOut as exc:
            recorder.crashed = True
            recorder.exit_code = 124
            recorder.crash_summary = f"LDRTimeout: {exc}"
            recorder.escaped_exception = exc
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            recorder.crashed = True
            recorder.exit_code = 1
            # Guarded, capped and secret-redacted: a raising __str__ must not
            # crash LDR, and the message must not carry a credential to the model.
            recorder.crash_summary = capture_error(exc)
            recorder.escaped_exception = exc
    finally:
        if alarm_enabled:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        recorder.stop()
    if recorder.escaped_exception is None and recorder.runner_exception is not None:
        recorder.escaped_exception = recorder.runner_exception
    recorder.finalize(
        success=recorder.exit_code == 0 and not recorder.crashed,
        escaped_exception=recorder.escaped_exception,
    )
    return recorder


def _entry_dirs(mode: str, target: str, rest: list[str] | None = None) -> list[str]:
    directories: list[str] = []
    if mode in ("run", "pytest"):
        file_part = target.split("::", 1)[0]
        if os.path.exists(file_part):
            directories.append(os.path.dirname(os.path.abspath(file_part)))
    elif mode == "module":
        real_target = target
        if target in _KNOWN_RUNNERS:
            real_target = next((item for item in (rest or []) if item and not item.startswith("-")), None)
        if real_target:
            try:
                top = real_target.split(".", 1)[0]
                spec = importlib.util.find_spec(top)
                if spec and spec.origin and top not in _KNOWN_RUNNERS:
                    directories.append(os.path.dirname(os.path.abspath(spec.origin)))
            except Exception:
                pass
    return directories


def _infer_runner(target: str) -> str:
    if "::" in target:
        return "pytest"
    file_part = target.split("::", 1)[0]
    if file_part.endswith(".py"):
        basename = os.path.basename(file_part)
        normalized = file_part.replace("\\", "/")
        if basename.startswith("test_") or "/tests/" in f"/{normalized}":
            return "pytest"
        return "run"
    return "module"


def _target_origin(mode: str, target: str, rest: list[str]) -> str | None:
    if mode in ("run", "pytest"):
        file_part = target.split("::", 1)[0]
        return os.path.realpath(file_part) if os.path.exists(file_part) else None
    module_target = target
    if target in _KNOWN_RUNNERS:
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


def _infer_project_root(mode: str, target: str, rest: list[str], cwd: str) -> str:
    origin = _target_origin(mode, target, rest)
    start = os.path.dirname(origin) if origin and os.path.isfile(origin) else (origin or cwd)
    fallback = current = os.path.realpath(start)
    generic_roots = {
        os.path.realpath(os.path.abspath(os.sep)),
        os.path.realpath(tempfile.gettempdir()),
    }
    while True:
        if current not in generic_roots and any(
            os.path.exists(os.path.join(current, marker)) for marker in _PROJECT_MARKERS
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


def _display_scope(paths: list[str], cwd: str) -> list[str]:
    """Return stable, deduplicated scope arguments for a command rerun from cwd."""
    displayed: list[str] = []
    for path in paths:
        value = path
        try:
            resolved = os.path.realpath(path)
            if os.path.commonpath((resolved, cwd)) == cwd:
                value = os.path.relpath(resolved, cwd)
        except (OSError, ValueError):
            pass
        if value not in displayed:
            displayed.append(value)
    return displayed


def _detect_production_roots(project_root: str) -> list[str]:
    """Detect common Python package roots without importing target code."""
    candidates: list[str] = []
    pyproject_path = os.path.join(project_root, "pyproject.toml")
    config: dict[str, Any] = {}
    try:
        with open(pyproject_path, "rb") as handle:
            config = tomllib.load(handle)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        pass

    tool = config.get("tool", {}) if isinstance(config, dict) else {}
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
    package_dir = (
        setuptools.get("package-dir", {}) if isinstance(setuptools, dict) else {}
    )
    if isinstance(package_dir, dict) and isinstance(package_dir.get(""), str):
        candidates.append(package_dir[""])
    package_find = (
        setuptools.get("packages", {}) if isinstance(setuptools, dict) else {}
    )
    package_find = (
        package_find.get("find", {}) if isinstance(package_find, dict) else {}
    )
    where = package_find.get("where", []) if isinstance(package_find, dict) else []
    if isinstance(where, str):
        where = [where]
    if isinstance(where, list):
        candidates.extend(item for item in where if isinstance(item, str))

    poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
    packages = poetry.get("packages", []) if isinstance(poetry, dict) else []
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, dict):
                continue
            source = package.get("from")
            include = package.get("include")
            if isinstance(source, str):
                candidates.append(source)
            elif isinstance(include, str):
                candidates.append(include.split(".", 1)[0])

    hatch = tool.get("hatch", {}) if isinstance(tool, dict) else {}
    build = hatch.get("build", {}) if isinstance(hatch, dict) else {}
    targets = build.get("targets", {}) if isinstance(build, dict) else {}
    wheel = targets.get("wheel", {}) if isinstance(targets, dict) else {}
    hatch_packages = wheel.get("packages", []) if isinstance(wheel, dict) else []
    if isinstance(hatch_packages, list):
        candidates.extend(item for item in hatch_packages if isinstance(item, str))

    src = os.path.join(project_root, "src")
    if os.path.isdir(src):
        candidates.append("src")
    try:
        entries = sorted(os.scandir(project_root), key=lambda entry: entry.name)
    except OSError:
        entries = []
    for entry in entries:
        if (
            entry.is_dir(follow_symlinks=False)
            and entry.name.lower() not in _PACKAGE_ROOT_EXCLUDES
            and os.path.isfile(os.path.join(entry.path, "__init__.py"))
        ):
            candidates.append(entry.name)

    discovered: list[str] = []
    for candidate in candidates:
        absolute = os.path.realpath(os.path.join(project_root, candidate))
        if os.path.isdir(absolute) and absolute not in discovered:
            discovered.append(absolute)
    roots: list[str] = []
    for candidate in sorted(discovered, key=lambda path: (len(path), path)):
        try:
            if any(os.path.commonpath((candidate, root)) == root for root in roots):
                continue
        except ValueError:
            pass
        roots.append(candidate)
    return roots


def _is_test_node(node: Node) -> bool:
    source = (node.source_id or node.file).replace("\\", "/").lower()
    basename = source.rsplit("/", 1)[-1]
    leaf = node.name.rsplit(".", 1)[-1]
    return (
        leaf.startswith("test_")
        or "/tests/" in f"/{source}"
        or basename.startswith("test_")
        or basename == "conftest.py"
    )


def _trace_has_production_frame(roots: list[Node], production_roots: list[str]) -> bool:
    for root in roots:
        for node in root.walk():
            if _is_test_node(node):
                continue
            filename = os.path.realpath(node.file) if node.file else ""
            for production_root in production_roots:
                try:
                    if (
                        filename
                        and os.path.commonpath((filename, production_root))
                        == production_root
                    ):
                        return True
                except ValueError:
                    continue
    return False


def _scope_setup(
    explicit: list[str] | None,
    project_root: str,
    entry_dirs: list[str],
    initial_cwd: str,
) -> tuple[list[str], list[str], list[str]]:
    detected = _detect_production_roots(project_root)
    warnings: list[str] = []
    if explicit:
        scope = list(explicit)
        missing = [item for item in explicit if not os.path.exists(item)]
        if missing:
            shown = ", ".join(repr(item) for item in missing)
            suggestion = _display_scope(detected, initial_cwd)
            suffix = (
                f" Detected production roots: {' '.join(suggestion)}."
                if suggestion
                else ""
            )
            warnings.append(f"Explicit --only path(s) do not exist: {shown}.{suffix}")
    else:
        scope = list(detected) or [project_root]
    for directory in entry_dirs:
        if directory not in scope:
            scope.append(directory)
    return scope, detected, warnings


def _rerun_payload(
    args,
    scope: list[str],
    initial_cwd: str,
    project_root: str,
) -> dict[str, Any]:
    displayed_project_root = project_root
    try:
        if os.path.commonpath((project_root, initial_cwd)) == initial_cwd:
            displayed_project_root = os.path.relpath(project_root, initial_cwd)
    except (OSError, ValueError):
        pass
    return {
        "runner": args.mode,
        "target": args.target,
        "arguments": redact_argv(list(args.rest)),
        "scope": _display_scope(scope, initial_cwd),
        "project_root": displayed_project_root,
    }


def _restore_frame_shortcut(args, parser, ldr_dir: str) -> None:
    """Restore a broad invocation for ``ldr inspect --frame-id N``."""
    result_path = os.path.join(ldr_dir, "last.json")
    try:
        with open(result_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        parser.error(f"--frame-id requires a readable {result_path}: {exc}")
    frame = next(
        (
            item for item in payload.get("frame_index", {}).get("frames", [])
            if item.get("id") == args.frame_id
        ),
        None,
    )
    if frame is None:
        parser.error(f"frame id {args.frame_id} is not present in {result_path}")
    suggestion = next(
        (
            item for item in payload.get("suggestions", [])
            if item.get("id") == args.frame_id
        ),
        None,
    )
    prior_inspection = payload.get("inspection", {})
    repeated_selector = (
        prior_inspection.get("requested_frame")
        if prior_inspection.get("requested_id") == args.frame_id
        else None
    )
    rerun = payload.get("rerun")
    if not isinstance(rerun, dict) or not rerun.get("target"):
        parser.error(
            "the previous result predates frame-id reruns; run a fresh broad trace"
        )
    if any("<secret" in str(argument) for argument in rerun.get("arguments", [])):
        parser.error(
            "the previous run's arguments contained credentials that LDR redacted, so it "
            "cannot rerun them; call inspect with the target, --frame and the real "
            "arguments instead of --frame-id"
        )
    args.frame = (
        suggestion.get("selector")
        if suggestion
        else repeated_selector or frame.get("frame_id")
    )
    args.target = rerun["target"]
    args.rest = list(rerun.get("arguments", []))
    args.only = list(rerun.get("scope", [])) or None
    args.runner = rerun.get("runner", "auto")
    args.project_root = rerun.get("project_root")


def _status(recorder: Recorder) -> dict[str, Any]:
    return {
        "ok": recorder.exit_code == 0 and not recorder.crashed,
        "exit_code": recorder.exit_code,
        "crashed": recorder.crashed,
        "crash_summary": recorder.crash_summary,
        "trace_truncated": recorder.truncated,
        "recorded_nodes": recorder.n_nodes,
        "overflow_transitions": recorder.dropped_nodes,
        "sampled_repetitions": recorder.sampled_nodes,
    }


def _node_state(node: Node | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
        "frame_id": node.frame_id,
        "inputs": node.in_,
        "output": node.out,
        "error": node.error,
        "handled": node.handled,
    }


def _recent_context(node: Node) -> list[dict[str, Any]]:
    recent = []
    for child in node.children[-8:]:
        recent.append(
            {
                "function": child.name,
                "frame_id": child.frame_id,
                "source_id": child.source_id,
                "location": child.location,
                "inputs": child.in_,
                "output": child.out,
                "error": child.error,
                "handled": child.handled,
            }
        )
    return recent


def _value_differences(store, baseline_id, change):
    if change.old is None or not change.old.frame_id or not change.node.frame_id:
        return []
    before = store.frame_values(baseline_id, change.old.frame_id)
    after = store.frame_values(store.run_id, change.node.frame_id)
    differences = []
    for field, (state, _) in compare_fields(change.old, change.node).items():
        if state != "different":
            continue
        if field == "out":
            phase, name = "return", "result"
        elif field.startswith("in."):
            phase, name = "entry", field[3:]
        else:
            continue
        left = before.get(phase, {}).get(name, {})
        right = after.get(phase, {}).get(name, {})
        if left.get("status") == right.get("status") == "complete":
            detail = first_difference(left["value"], right["value"])
            if detail is not None:
                path, old, new = detail
                differences.append({"field": field, "path": path, "before": capture(old), "after": capture(new)})
    return differences


def _finding_payload(changes, store=None, baseline_id=None) -> list[dict[str, Any]]:
    findings = []
    for position, change in enumerate(sorted(changes, key=rank)[:25], 1):
        findings.append(
            {
                "rank": position,
                "kind": change.kind,
                "path": change.path,
                "function": change.node.name,
                "frame_id": change.node.frame_id,
                "source_id": change.node.source_id,
                "location": change.node.location,
                "callsite": change.node.callsite,
                "before": _node_state(change.old),
                "after": _node_state(change.node),
            }
        )
        if store is not None and store.max_value_bytes and baseline_id:
            details = _value_differences(store, baseline_id, change)
            if details:
                findings[-1]["value_changes"] = details
    return findings


def _failure_payload(roots: list[Node]) -> list[dict[str, Any]]:
    failures = []

    def visit(node: Node, path: list[str], frame_path: list[str]):
        here = path + [node.name]
        here_frames = frame_path + [node.frame_id]
        if node.error is not None and not node.handled:
            failures.append(
                {
                    "path": here,
                    "frame_path": here_frames,
                    "function": node.name,
                    "frame_id": node.frame_id,
                    "source_id": node.source_id,
                    "location": node.location,
                    "callsite": node.callsite,
                    "error": node.error,
                    "inputs": node.in_,
                    "recent_calls": _recent_context(node),
                }
            )
        for child in node.children:
            visit(child, here, here_frames)

    for root in roots:
        visit(root, [], [])
    return failures


def _frame_index(roots: list[Node], limit: int = 5000) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    functions: dict[tuple[str, str], dict[str, Any]] = {}
    next_id = 0

    def visit(node: Node, parent_id: str | None) -> None:
        nonlocal next_id
        next_id += 1
        if len(frames) < limit:
            frames.append(
                {
                    "id": next_id,
                    "frame_id": node.frame_id,
                    "parent_id": parent_id,
                    "function": node.name,
                    "location": node.location,
                    "error": node.error,
                    "handled": node.handled,
                    **({"timing": node.timing} if node.timing is not None else {}),
                }
            )
        key = (node.source_id, node.name)
        summary = functions.setdefault(
            key,
            {
                "function": node.name,
                "source_id": node.source_id,
                "location": node.location,
                "recorded_calls": 0,
                "sample_frame_ids": [],
                "inspect_selector": f"{node.code_id}#*",
                "error_count": 0,
            },
        )
        summary["recorded_calls"] += 1
        if node.error is not None:
            summary["error_count"] += 1
        samples = summary["sample_frame_ids"]
        if node.frame_id and (not samples or node.frame_id != samples[-1]):
            if len(samples) < 4:
                samples.append(node.frame_id)
            else:
                samples[-1] = node.frame_id
        for child in node.children:
            visit(child, node.frame_id or parent_id)

    total = sum(root.count() for root in roots)
    for root in roots:
        visit(root, None)
    ordered_functions = sorted(
        functions.values(),
        key=lambda item: (item["source_id"], item["location"], item["function"]),
    )
    return {
        "frames": frames,
        "functions": ordered_functions,
        "total": total,
        "truncated": total > len(frames),
    }


def _limit_text(text: str, max_lines: int, max_chars: int = 40_000) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        head = max(5, (max_lines * 2) // 3)
        tail = max_lines - head - 1
        omitted = len(lines) - head - tail
        lines = lines[:head] + [f"[... {omitted} lines omitted ...]"] + lines[-tail:]
    text = "\n".join(lines)
    if len(text) > max_chars:
        head = (max_chars * 2) // 3
        tail = max_chars - head - 80
        text = text[:head] + f"\n[... {len(text) - head - tail} characters omitted ...]\n" + text[-tail:]
    return text


def _redaction_note(count: int) -> str:
    if not count:
        return ""
    return f"REDACTED: {count} value(s) redacted, not compared.\n"


def _write_result(text: str, payload: dict[str, Any], ldr_dir: str = LDR_DIR) -> tuple[str, str]:
    os.makedirs(ldr_dir, exist_ok=True)
    text_path = os.path.join(ldr_dir, "last.txt")
    json_path = os.path.join(ldr_dir, "last.json")
    atomic_text(text_path, text)
    atomic_json(json_path, payload)
    return text_path, json_path


def _print_result(text: str) -> None:
    encoding = sys.stdout.encoding
    if encoding:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    print(text)


def _show_saved(argv, ldr_dir):
    parser = argparse.ArgumentParser(prog="ldr show")
    parser.add_argument("frame", nargs="?", help="stored frame address, or omit to list frames")
    parser.add_argument("--run-id", help="stored run ID (defaults to the latest run)")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--boundaries", action="store_true", help="list saved library calls")
    parser.add_argument(
        "--environment", action="store_true", help="show the run's recorded environment"
    )
    args = parser.parse_args(argv)
    if not 0 <= args.offset < 2**63 or not 0 < args.limit < 2**63:
        parser.error("--offset must be nonnegative and --limit must be positive SQLite integers")
    if args.boundaries and args.environment:
        parser.error("--boundaries and --environment show different evidence; pick one")
    if (args.boundaries or args.environment) and args.frame is not None:
        parser.error("--boundaries and --environment take no frame argument")
    try:
        if args.environment:
            run_id, evidence = TraceStore.read_environment(ldr_dir, args.run_id)
        elif args.boundaries:
            run_id, evidence = TraceStore.read_boundaries(
                ldr_dir, args.run_id, offset=args.offset, limit=args.limit
            )
        else:
            run_id, evidence = TraceStore.read_deep(
                ldr_dir, args.run_id, args.frame, offset=args.offset, limit=args.limit
            )
    except sqlite3.Error as exc:
        print(f"Could not read saved capture ({capture_error(exc)}).", file=sys.stderr)
        return 2
    if run_id is None and (args.frame is not None or args.run_id is not None):
        print("No matching saved run.", file=sys.stderr)
        return 2
    if args.environment and evidence is None:
        print(
            f"No environment was recorded for run {run_id}." if run_id else "No saved runs.",
            file=sys.stderr,
        )
        return 2
    if evidence is None:
        print(f"No saved deep capture for {args.frame} in run {run_id}.", file=sys.stderr)
        return 2
    if args.json:
        key = (
            "environment" if args.environment
            else "boundaries" if args.boundaries
            else ("frames" if args.frame is None else "frame")
        )
        print(json.dumps({"run_id": run_id, key: evidence}))
        return 0
    lines = [f"Run {run_id}" if run_id else "No saved runs."]
    if args.environment:
        python, system = evidence["python"], evidence["platform"]
        lines.append(f"Python {python['version']} ({python['implementation']})")
        lines.append(f"Platform: {system['system']} {system['release']} ({system['tag']})")
        lines.append(f"Packages ({len(evidence['packages'])}):")
        lines.extend(f"  {name} {version}" for name, version in evidence["packages"].items())
        lines.append(
            f"Environment variable names ({len(evidence['variables'])}); "
            "values are never recorded:"
        )
        lines.append("  " + ", ".join(evidence["variables"]))
    elif args.boundaries:
        for call in evidence:
            site = call["caller"]
            lines.append(
                f"{call['module']}.{call['function']} {call['outcome']} "
                f"at {site['file']}:{site['line']}"
            )
            lines.extend(f"  {name} = {_show_value(value)}" for name, value in call["arguments"])
            if not call["arguments_complete"]:
                lines.append("  <some arguments omitted by the capture budget>")
            result = call.get("result", call.get("exception"))
            lines.append(f"  {call['outcome']} {_show_value(result)}")
        if not evidence and run_id:
            lines.append("No saved library calls on this page.")
    elif args.frame is None:
        lines.extend(
            f"{frame['frame_id']}  ({frame['retained']}/{frame['observed']} snapshots)"
            for frame in evidence
        )
        if not evidence and run_id:
            lines.append("No deep captures on this page.")
    else:
        lines.append(
            f"{args.frame}  ({evidence['retained']}/{evidence['observed']} snapshots)"
        )
        for step in evidence["steps"]:
            lines.append(f"Step {step['step']}, line {step['line']} ({step['event']})")
            for name, local in step["locals"]:
                lines.append(f"  {name} = {_show_value(local)}")
            if not step["locals_complete"]:
                lines.append("  <some locals omitted by the capture budget>")
    _print_result(_limit_text("\n".join(lines), max_lines=sys.maxsize))
    return 0


def _show_value(evidence):
    if evidence["status"] == "complete":
        return json.dumps(evidence["value"], ensure_ascii=False)
    return f"<{evidence['status']}>"


def _noise_run(
    args,
    scope: list[str],
    invocation: dict[str, Any],
    recorder: Recorder,
    ldr_dir: str,
) -> list[Node]:
    os.makedirs(ldr_dir, exist_ok=True)
    fd, noise_path = tempfile.mkstemp(prefix="noise-", suffix=".json", dir=ldr_dir)
    os.close(fd)
    os.unlink(noise_path)
    # This process is already the child: the noise run must not start another.
    environment = dict(os.environ, LDR_NOISE_RUN=noise_path, **{CHILD_MARKER: "1"})
    command = [
        sys.executable,
        "-m",
        "ldr.cli",
        args.mode,
        args.target,
        "--project-root",
        args.project_root,
        "--only",
        *scope,
        "--max-nodes",
        str(args.max_nodes),
        "--max-calls-per-function",
        str(args.max_calls_per_function),
        "--max-lines",
        str(args.max_lines),
        "--timeout",
        str(args.timeout),
        "--",
        *args.rest,
    ]
    try:
        completed = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            timeout=(args.timeout + 5) if args.timeout > 0 else None,
            cwd=args.initial_cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("noise run timed out; snapshot was not saved") from exc
    try:
        if not os.path.exists(noise_path):
            detail = completed.stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"noise run produced no trace: {detail}")
        roots, envelope = load_trace(noise_path)
        noise_status = envelope.get("status", {})
        if noise_status.get("exit_code") != recorder.exit_code:
            raise RuntimeError(
                "noise run changed runner status; snapshot was not saved "
                f"({recorder.exit_code} then {noise_status.get('exit_code')})"
            )
        return roots
    finally:
        try:
            os.unlink(noise_path)
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    unsupported = python_version_message()
    if unsupported:
        print(unsupported, file=sys.stderr)
        return 2
    # Set by the parent that started this process. The target must not see either.
    is_child = os.environ.pop(CHILD_MARKER, None) == "1"
    selected_by = os.environ.pop(SELECTED_BY, None) or RUNNING
    total_started = time.perf_counter()
    initial_cwd = os.path.realpath(os.getcwd())
    ldr_dir = os.path.join(initial_cwd, LDR_DIR)
    argv = list(sys.argv[1:] if argv is None else argv)
    launch_argv = list(argv)
    if argv and argv[0] == "show":
        return _show_saved(argv[1:], ldr_dir)
    register_argv_secrets(argv)
    timeout_requested = any(a == "--timeout" or a.startswith("--timeout=") for a in argv)
    passthrough: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, passthrough = argv[:index], argv[index + 1 :]

    parser = argparse.ArgumentParser(prog="ldr")
    parser.add_argument("--version", action="version", version=f"ldr {__version__}")
    parser.add_argument("mode", choices=["run", "pytest", "module", "verify", "inspect", "show"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--runner", choices=["auto", "run", "pytest", "module"], default="auto")
    parser.add_argument(
        "--only", nargs="+", default=None,
        help="paths to trace (default: inferred project root)",
    )
    parser.add_argument("--project-root", help="stable source-identity root (normally inferred)")
    parser.add_argument("--golden", action="store_true", help="replace this invocation's snapshot")
    parser.add_argument("--diff", action="store_true", help="compare against this invocation's snapshot")
    parser.add_argument("--snapshot-name", help="explicit snapshot key")
    parser.add_argument("--learn-volatility", action="store_true", help="run twice and mask changing fields")
    parser.add_argument("--fail-on-change", action="store_true")
    parser.add_argument("--color", action="store_true")
    parser.add_argument("--full", action="store_true", help="disable trace pruning")
    parser.add_argument("--max-nodes", type=int, default=5000)
    parser.add_argument("--max-calls-per-function", type=int, default=8)
    parser.add_argument("--max-lines", type=int, default=200)
    parser.add_argument(
        "--frame",
        help="portable frame id, or a function selector ending in #*, for a focused re-run",
    )
    parser.add_argument(
        "--frame-id",
        type=int,
        help="short frame id from the latest result; restores and reruns its invocation",
    )
    parser.add_argument("--max-state-steps", type=int, default=200)
    parser.add_argument(
        "--deep", action="append", default=[], metavar="FUNCTION",
        help="save locals for a selected function (repeatable)",
    )
    parser.add_argument(
        "--boundary", action="append", default=[], metavar="MODULE",
        help="save direct calls from the project into a Python module (repeatable)",
    )
    parser.add_argument("--timeout", type=float, default=300)
    args, unknown = parser.parse_known_args(argv)
    args.rest = list(args.rest) + unknown + passthrough
    if args.golden and args.diff:
        parser.error("--golden and --diff are mutually exclusive")
    if args.max_lines < 20:
        parser.error("--max-lines must be at least 20")
    if args.max_nodes < 1:
        parser.error("--max-nodes must be at least 1")
    if args.max_calls_per_function < 1:
        parser.error("--max-calls-per-function must be at least 1")
    if args.max_state_steps < 1:
        parser.error("--max-state-steps must be at least 1")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        parser.error("--timeout must be a finite non-negative number")

    requested_mode = args.mode
    if args.deep and (
        requested_mode not in {"run", "pytest", "module", "verify"}
        or any(not name.strip() for name in args.deep)
    ):
        parser.error("--deep requires a function name and a recording command")
    if args.boundary and (
        requested_mode not in {"run", "pytest", "module", "verify"}
        or any(not all(part.isidentifier() for part in name.split(".")) for name in args.boundary)
    ):
        parser.error("--boundary requires a dotted module name and a recording command")
    if args.frame_id is not None:
        if requested_mode != "inspect":
            parser.error("--frame-id is only valid with inspect")
        if args.frame_id < 1:
            parser.error("--frame-id must be a positive integer")
        if args.target is not None or args.frame is not None or args.rest:
            parser.error("inspect --frame-id does not accept a target, --frame, or arguments")
        _restore_frame_shortcut(args, parser, ldr_dir)
    elif args.target is None:
        parser.error("target is required (or use inspect --frame-id N)")

    if requested_mode in ("verify", "inspect"):
        args.mode = _infer_runner(args.target) if args.runner == "auto" else args.runner
    elif args.runner != "auto":
        parser.error("--runner is only valid with verify or inspect")
    if requested_mode == "inspect":
        if not args.frame:
            parser.error("inspect requires --frame or --frame-id from a previous LDR result")
        if args.golden or args.diff or args.learn_volatility:
            parser.error("inspect cannot save or compare snapshots")
    elif args.frame or args.frame_id is not None:
        parser.error("--frame and --frame-id are only valid with inspect")

    if args.project_root:
        project_root = os.path.realpath(args.project_root)
        if not os.path.isdir(project_root):
            parser.error("--project-root must be an existing directory")
    else:
        project_root = _infer_project_root(args.mode, args.target, args.rest, initial_cwd)
    args.project_root = project_root
    args.initial_cwd = initial_cwd

    if not is_child:
        # The target always runs in a child process, in the project's interpreter.
        return run_in_child(launch_argv, project_root)

    if args.mode == "pytest" and importlib.util.find_spec("pytest") is None:
        message = "LDR cannot run pytest because pytest is not installed in this Python environment."
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": {"ok": False, "exit_code": 2, "infrastructure_error": message},
            "findings": [],
            "failures": [],
        }
        _write_result(message, payload, ldr_dir)
        print(message, file=sys.stderr)
        return 2

    scope, detected_roots, scope_warnings = _scope_setup(
        list(args.only) if args.only else None,
        project_root,
        _entry_dirs(args.mode, args.target, args.rest),
        initial_cwd,
    )
    invocation = invocation_descriptor(
        args.mode,
        args.target,
        args.rest,
        scope,
        cwd=initial_cwd,
        project_root=project_root,
    )
    try:
        key = invocation_key(invocation, args.snapshot_name)
    except ValueError as exc:
        parser.error(str(exc))
    golden_path = snapshot_path(ldr_dir, key)
    if requested_mode == "verify" and not args.golden and not args.diff:
        args.diff = os.path.exists(golden_path)
        args.golden = not args.diff
    if args.learn_volatility and not args.golden:
        parser.error("--learn-volatility is only valid while saving a snapshot")

    store = None
    storage_warning = None
    try:
        settings = storage_settings(
            project_root, deep=bool(args.deep), boundary=bool(args.boundary)
        )
        store = TraceStore.start(ldr_dir, invocation, *settings)
    except (OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        storage_warning = capture_error(exc)
        if args.deep or args.boundary:
            parser.error(f"Could not start optional capture ({storage_warning})")

    recorder_options = {
        "scope": scope,
        "max_nodes": args.max_nodes,
        "project_root": project_root,
        "display_root": initial_cwd,
        "focus_frame": args.frame if requested_mode == "inspect" else None,
        "max_state_steps": args.max_state_steps,
        "max_calls_per_function": args.max_calls_per_function,
        "deep_functions": args.deep,
        "boundary_modules": args.boundary,
    }
    pytest_capture = None
    # Read before the target runs, so the record is the environment it started in.
    environment = None
    try:
        environment = run_environment.capture_environment()
    except Exception as exc:
        scope_warnings.append(f"Environment record unavailable ({capture_error(exc)}).")

    def go(recorder):
        nonlocal pytest_capture
        if args.mode == "run":
            sys.argv = [args.target] + args.rest
            runpy.run_path(args.target, run_name="__main__")
        elif args.mode == "module":
            sys.argv = [args.target] + args.rest
            runpy.run_module(args.target, run_name="__main__", alter_sys=True)
        else:
            import pytest

            from .pytest_capture import ControllerCapture

            pytest_capture = ControllerCapture(recorder, store, recorder_options)
            code = pytest.main(
                [args.target, *args.rest, "-q", "-p", "no:cacheprovider", "-p", "ldr.pytest_worker"],
                plugins=[pytest_capture],
            )
            if int(code) != 0:
                raise SystemExit(int(code))

    execution_started = time.perf_counter()
    try:
        recorder = _record(
            go,
            timeout=args.timeout,
            defer_start=args.mode == "pytest",
            call_stream=CallStream(store, "main") if store is not None else None,
            **recorder_options,
        )
    except (RuntimeError, ValueError) as exc:
        message = f"LDR could not start tracing: {exc}"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": {"ok": False, "exit_code": 2, "infrastructure_error": message},
            "invocation": invocation,
            "findings": [],
            "failures": [],
        }
        _write_result(message, payload, ldr_dir)
        print(message, file=sys.stderr)
        return 2
    execution_finished = time.perf_counter()

    missing_workers = pytest_capture.merge() if pytest_capture and pytest_capture.workers else []
    status = _status(recorder)
    if pytest_capture and pytest_capture.workers:
        status["trace_workers"] = sorted(set(pytest_capture.workers) - set(missing_workers))
    if missing_workers:
        status["missing_trace_workers"] = missing_workers
        scope_warnings.append(f"Missing trace from pytest workers: {', '.join(missing_workers)}.")
    status["timing"] = {
        "execution_seconds": round(execution_finished - execution_started, 6),
    }
    status["redacted_values"] = count_redacted(recorder.roots)
    status["commit"] = git_state(project_root)
    if environment is not None:
        status["environment"] = run_environment.summarize(environment)
    status["interpreter"] = {
        "path": sys.executable,
        "version": platform.python_version(),
        "selected_by": selected_by,
    }
    if store is not None:
        status["run_id"] = store.run_id
        try:
            if environment is not None:
                try:
                    store.save_environment(environment)
                except (OSError, sqlite3.Error) as exc:
                    # The record is secondary evidence: do not lose the call stream over it.
                    scope_warnings.append(
                        f"Environment record could not be saved ({capture_error(exc)})."
                    )
            if not pytest_capture or not pytest_capture.workers:
                recorder.call_stream.finish()
                store.save_worker("main", recorder.roots, {})
            stream_summaries = store.read_stream(store.run_id)
            status["call_stream"] = {
                key: sum(summary[key] for summary in stream_summaries)
                for key in ("started", "stored", "unfinished", "uncomparable", "write_failed")
            }
            status["call_stream"]["expected_workers"] = len(pytest_capture.workers) if pytest_capture and pytest_capture.workers else 1
            status["call_stream"]["summaries"] = len(stream_summaries)
            status["call_stream"]["volatile_baseline"] = bool(args.learn_volatility)
            status["call_stream"]["capture_lines"] = store.capture_lines
            if store.max_value_bytes:
                status["call_stream"]["value_bytes"] = store.value_bytes_used()
                status["call_stream"]["max_value_bytes"] = store.max_value_bytes
            if args.deep:
                totals = store.deep_totals()
                totals.update(
                    (key, sum(summary["deep"][key] for summary in stream_summaries))
                    for key in ("matched", "unfinished", "observed")
                )
                status["deep_capture"] = {
                    "functions": args.deep,
                    "max_bytes": store.max_deep_bytes,
                    **totals,
                }
            if args.boundary:
                status["boundary_capture"] = {
                    "modules": [redact(module)[0] for module in args.boundary],
                    "max_bytes": store.max_boundary_bytes,
                    "bytes": store.boundary_bytes_used(),
                    **{
                        key: sum(summary["boundaries"][key] for summary in stream_summaries)
                        for key in ("started", "stored", "unfinished", "write_failed")
                    },
                }
            store.finish(status)
        except (OSError, sqlite3.Error) as exc:
            storage_warning = capture_error(exc)
    if storage_warning:
        status["storage_warning"] = storage_warning
        scope_warnings.append(f"SQLite trace storage: {storage_warning}")
    recorded_scope = _display_scope(scope, initial_cwd)
    project_calls = sum(
        1 for root in recorder.roots for node in root.walk() if not _is_test_node(node)
    )
    if project_calls == 0:
        scope_warnings.append(
            "No calls from project code were recorded. If the code you expected lives "
            "elsewhere, rerun with --only <dir> to name it, or --project-root <dir> to "
            f"set the project root. Recorded scope: {', '.join(recorded_scope) or '(none)'}."
        )
    redaction_note = (
        f"COMMIT: {describe_commit(status['commit'])}\n"
        f"SCOPE: {', '.join(recorded_scope) or '(none)'}; "
        f"{project_calls} project call(s) recorded\n"
        + _redaction_note(status["redacted_values"])
    )
    stream_status = status.get("call_stream")
    if stream_status:
        redaction_note += (
            f"CALL STREAM: {stream_status['stored']} of {stream_status['started']} call(s) stored individually, "
            f"{stream_status['unfinished']} unfinished, {stream_status['uncomparable']} with incomparable values.\n"
        )
        if stream_status["write_failed"] or stream_status["summaries"] != stream_status["expected_workers"]:
            scope_warnings.append("Some call stream records could not be written.")
    if deep_status := status.get("deep_capture"):
        redaction_note += (
            f"DEEP CAPTURE: {deep_status['retained']}/{deep_status['observed']} snapshots "
            f"saved from {deep_status['frames']}/{deep_status['matched']} matched calls "
            f"({deep_status['unfinished']} unfinished). "
            f"Read with ldr show --run-id {store.run_id}\n"
        )
    if boundary_status := status.get("boundary_capture"):
        redaction_note += (
            f"LIBRARY CALLS: {boundary_status['stored']}/{boundary_status['started']} saved "
            f"({boundary_status['unfinished']} unfinished). "
            f"Read with ldr show --boundaries --run-id {store.run_id}\n"
        )
        if boundary_status["write_failed"]:
            scope_warnings.append("Some library call records could not be written.")
    if (
        recorder.exit_code != 0
        and detected_roots
        and not _trace_has_production_frame(recorder.roots, detected_roots)
    ):
        suggested_scope = _display_scope(
            detected_roots + _entry_dirs(args.mode, args.target, args.rest),
            initial_cwd,
        )
        command = shlex.join(
            ["ldr", args.mode, args.target, "--only", *dict.fromkeys(suggested_scope)]
        )
        scope_warnings.append(
            "The trace captured no frames from a detected production package. "
            f"Try: {command}"
        )
    noise_path = os.environ.get("LDR_NOISE_RUN")
    if noise_path:
        if missing_workers:
            raise SystemExit(recorder.exit_code or 2)
        save_trace(noise_path, recorder.roots, invocation=invocation, status=status)
        return recorder.exit_code

    if requested_mode == "inspect":
        inspected = [
            node
            for root in recorder.roots
            for node in root.walk()
            if (
                f"{node.code_id}#" == args.frame[:-1]
                if args.frame.endswith("#*")
                else node.frame_id == args.frame
            )
        ]
        frame_index_payload = _frame_index(recorder.roots)
        display_scope = _display_scope(scope, initial_cwd)
        inspected_frames = [node.to_dict() for node in inspected]
        if store is not None and store.max_value_bytes:
            try:
                for frame in inspected_frames:
                    frame["captured_values"] = store.frame_values(store.run_id, frame["frame_id"])
            except sqlite3.Error:
                scope_warnings.append("Could not read stored structural values for inspection.")
        text = render_inspection(
            inspected,
            args.frame,
            args.max_lines,
            command_target=args.target,
            scope=display_scope,
            frame_index=frame_index_payload,
        )
        run_note = (
            f"RUN STATUS: {'PASSED' if recorder.exit_code == 0 else 'FAILED'} "
            f"(exit {recorder.exit_code})\n\n"
        ) + redaction_note
        if scope_warnings:
            run_note += "SCOPE WARNING\n" + "\n".join(f"- {warning}" for warning in scope_warnings) + "\n\n"
        status["timing"].update(
            {
                "postprocess_seconds": round(time.perf_counter() - execution_finished, 6),
                "total_seconds": round(time.perf_counter() - total_started, 6),
            }
        )
        run_note += (
            "TIMING: "
            f"execution={status['timing']['execution_seconds']:.3f}s "
            f"postprocess={status['timing']['postprocess_seconds']:.3f}s "
            f"total={status['timing']['total_seconds']:.3f}s\n\n"
        )
        text = _limit_text(run_note + text, args.max_lines)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "invocation": invocation,
            "rerun": _rerun_payload(args, scope, initial_cwd, project_root),
            "status": status,
            "inspection": {
                "requested_id": args.frame_id,
                "requested_frame": args.frame,
                "matched": bool(inspected),
                "match_count": len(inspected),
                "frames": inspected_frames,
            },
            "frame_index": frame_index_payload,
            "findings": [],
            "failures": _failure_payload(recorder.roots),
        }
        text_path, json_path = _write_result(text, payload, ldr_dir)
        _print_result(text)
        print(
            f"\n[ldr] agent result: {os.path.relpath(json_path, initial_cwd)}; "
            f"text: {os.path.relpath(text_path, initial_cwd)}",
            file=sys.stderr,
        )
        if recorder.exit_code != 0:
            return recorder.exit_code
        return 0 if inspected else 2

    changes = []
    baseline_sampled = 0
    unstable: list[dict[str, str]] = []
    comparison_unknown = False
    environment_comparison = None
    stream_changes = []
    findings = []
    snapshot_action = "none"
    frame_index_payload = _frame_index(recorder.roots)
    suggestions = rank_inspections(recorder.roots, frame_index_payload)
    display_scope = _display_scope(scope, initial_cwd)
    rerun = _rerun_payload(args, scope, initial_cwd, project_root)
    try:
        if missing_workers and (args.golden or args.diff):
            comparison_unknown = True
            snapshot_action = "unknown"
            text = "COMPARISON UNKNOWN: at least one pytest worker trace is missing."
        elif args.golden:
            if args.learn_volatility:
                second = _noise_run(args, scope, invocation, recorder, ldr_dir)
                for left, right in align(recorder.roots, second):
                    if left is not None and right is not None:
                        mark_unstable(left, right)
            save_trace(golden_path, recorder.roots, invocation=invocation, status=status)
            snapshot_action = "saved"
            text = render_run(
                recorder.roots,
                args.color,
                recorder.truncated,
                args.full,
                args.max_lines,
                recorder.dropped_nodes,
                recorder.sampled_nodes,
                frame_index=frame_index_payload,
                suggestions=suggestions if recorder.exit_code != 0 else None,
                command_target=args.target,
                scope=display_scope,
                status=status,
                max_nodes=args.max_nodes,
            )
            volatility = sum(bool(node.volatile) for root in recorder.roots for node in root.walk())
            text += f"\n\nSnapshot saved for this invocation: {os.path.relpath(golden_path, initial_cwd)}"
            text += (
                f"\nBaseline recorded from a run with outcome {describe_outcome(status)} "
                f"at commit {describe_commit(status['commit'])}."
            )
            if args.learn_volatility:
                text += f"\n{volatility} node(s) contain field-level volatility masks."
                unstable = unstable_fields(recorder.roots)
                if unstable:
                    text += "\n\n" + render_unstable(unstable)
        elif args.diff:
            if not os.path.exists(golden_path):
                raise FileNotFoundError(
                    f"No snapshot for this invocation ({key}). Run verify or --golden first."
                )
            old, envelope = load_trace(golden_path)
            if envelope.get("invocation") not in ({}, invocation):
                raise ValueError("snapshot invocation metadata does not match this command")
            snapshot_schema = envelope.get("schema_version", 1)
            baseline_status = envelope.get("status", {})
            if ("trace_workers" in status) != ("trace_workers" in baseline_status):
                comparison_unknown = True
                snapshot_action = "unknown"
                text = (
                    "COMPARISON UNKNOWN: the baseline and this run use different "
                    "pytest capture modes. Re-record the baseline with --golden."
                )
            elif snapshot_schema < SCHEMA_VERSION:
                comparison_unknown = True
                snapshot_action = "unknown"
                text = (
                    "COMPARISON UNKNOWN: this snapshot was recorded by an older LDR "
                    f"(schema {snapshot_schema}, current {SCHEMA_VERSION}). Its values are "
                    "cut-off previews, so equal or different cannot be said. "
                    "Re-record it with --golden, then compare again."
                )
            else:
                for left, right in align(old, recorder.roots):
                    changes.extend(diff_tree(left, right))
                if (
                    not changes and store is not None and stream_status
                    and baseline_status.get("run_id") and baseline_status.get("call_stream")
                    and not baseline_status.get("call_stream", {}).get("volatile_baseline")
                    and stream_status["summaries"] == stream_status["expected_workers"]
                ):
                    try:
                        baseline_stream = store.read_stream(baseline_status["run_id"])
                        if len(baseline_stream) == baseline_status["call_stream"]["expected_workers"]:
                            stream_changes = changed_functions(baseline_stream, stream_summaries)
                    except sqlite3.Error:
                        scope_warnings.append("Could not read baseline call stream fingerprints.")
                snapshot_action = "compared"
            if comparison_unknown:
                pass
            elif changes:
                headline = (
                    "BEST GUESS, RANKED"
                    if any(change.kind != EQUAL_READABLE for change in changes)
                    else "EQUAL IN READABLE PARTS ONLY (not proven equal)"
                )
                text = headline + "\n" + render_diff(changes, args.color)
                try:
                    findings = _finding_payload(changes, store, baseline_status.get("run_id"))
                except sqlite3.Error:
                    scope_warnings.append("Could not read structural values for comparison.")
                details = [
                    f"{finding['function']} {value['field']}{value['path']}: {value['before']} -> {value['after']}"
                    for finding in findings for value in finding.get("value_changes", [])
                ]
                if details:
                    text += "\n\nSTORED VALUE DIFFERENCES\n" + "\n".join(details)
                text += "\n\nCALL CHAIN\n" + render_call_chain(recorder.roots, changes, args.color)
            elif stream_changes:
                text = "CALL STREAM: fingerprints changed for " + ", ".join(stream_changes[:10]) + "."
            else:
                text = "No behavioural change vs snapshot."
            unstable = unstable_fields(old)
            if unstable:
                text += "\n\n" + render_unstable(unstable)
            baseline_sampled = baseline_status.get("sampled_repetitions", 0)
            environment_note = ""
            if environment is not None:
                baseline_record = None
                if (
                    store is not None
                    and baseline_status.get("run_id")
                    and baseline_status.get("environment")
                ):
                    try:
                        baseline_record = store.load_environment(baseline_status["run_id"])
                    except (OSError, sqlite3.Error, ValueError):
                        scope_warnings.append("Could not read the baseline environment record.")
                environment_comparison = run_environment.comparison(
                    baseline_status.get("environment"),
                    status["environment"],
                    baseline_record,
                    environment,
                )
                environment_note = run_environment.render_comparison(
                    environment_comparison, status["environment"]
                )
            if baseline_status:
                text = (
                    f"BASELINE: outcome {describe_outcome(baseline_status)}, commit "
                    f"{describe_commit(baseline_status.get('commit'))}. "
                    f"THIS RUN: outcome {describe_outcome(status)}, commit "
                    f"{describe_commit(status['commit'])}.\n"
                    + environment_note
                    + "\n"
                    + text
                )
            elif environment_note:
                text = environment_note + "\n" + text
        else:
            text = render_run(
                recorder.roots,
                args.color,
                recorder.truncated,
                args.full,
                args.max_lines,
                recorder.dropped_nodes,
                recorder.sampled_nodes,
                frame_index=frame_index_payload,
                suggestions=suggestions if recorder.exit_code != 0 else None,
                command_target=args.target,
                scope=display_scope,
                status=status,
                max_nodes=args.max_nodes,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        text = f"LDR snapshot error: {exc}"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "invocation": invocation,
            "status": {**status, "ldr_error": str(exc)},
            "snapshot": {"key": key, "path": golden_path, "action": "error"},
            "findings": [],
            "failures": _failure_payload(recorder.roots),
        }
        _write_result(text, payload, ldr_dir)
        print(text, file=sys.stderr)
        return 2

    note = ""
    if recorder.crashed:
        note = f"RUN STATUS: CRASHED (exit {recorder.exit_code})\n{recorder.crash_summary}\n\n"
    elif recorder.exit_code != 0:
        note = f"RUN STATUS: FAILED (exit {recorder.exit_code})\n"
        if not _failure_payload(recorder.roots):
            note += (
                "NOTE: the runner failed outside the traced function scope; "
                "the trace cannot localize that failure.\n"
            )
        note += "\n"
    else:
        note = "RUN STATUS: PASSED\n\n"
    note += redaction_note
    if recorder.sampled_nodes or baseline_sampled:
        note += (
            f"DIAGNOSTIC SAMPLE: calls beyond the first {args.max_calls_per_function} "
            "per function are not compared individually unless sampled.\n"
        )
    if timeout_requested and not hasattr(signal, "SIGALRM"):
        # Windows has no SIGALRM, so the in-process timeout cannot fire.
        note += (
            "WARNING: --timeout is not enforced on this platform (no SIGALRM); "
            "a hung run will not be stopped.\n\n"
        )
    if scope_warnings:
        note += "SCOPE WARNING\n" + "\n".join(f"- {warning}" for warning in scope_warnings) + "\n\n"
    status["timing"].update(
        {
            "postprocess_seconds": round(time.perf_counter() - execution_finished, 6),
            "total_seconds": round(time.perf_counter() - total_started, 6),
        }
    )
    note += (
        "TIMING: "
        f"execution={status['timing']['execution_seconds']:.3f}s "
        f"postprocess={status['timing']['postprocess_seconds']:.3f}s "
        f"total={status['timing']['total_seconds']:.3f}s\n\n"
    )
    text = _limit_text(note + text, args.max_lines)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "invocation": invocation,
        "rerun": rerun,
        "status": status,
        "snapshot": {"key": key, "path": golden_path, "action": snapshot_action},
        "behavior": {
            "changed": bool(stream_changes) or any(change.kind != EQUAL_READABLE for change in changes),
            "stream_changed_functions": stream_changes,
            "finding_count": len(changes),
            "equal_in_readable_parts_only": sum(
                change.kind == EQUAL_READABLE for change in changes
            ),
            "unstable_fields": unstable[:50],
        },
        "environment_comparison": environment_comparison,
        "frame_index": frame_index_payload,
        "suggestions": suggestions,
        "scope": {
            "detected_production_roots": _display_scope(detected_roots, initial_cwd),
            "recorded": recorded_scope,
            "project_calls": project_calls,
            "warnings": scope_warnings,
        },
        "findings": findings or _finding_payload(changes),
        "failures": _failure_payload(recorder.roots),
    }
    text_path, json_path = _write_result(text, payload, ldr_dir)
    _print_result(text)
    print(
        f"\n[ldr] agent result: {os.path.relpath(json_path, initial_cwd)}; "
        f"text: {os.path.relpath(text_path, initial_cwd)}",
        file=sys.stderr,
    )

    if recorder.exit_code != 0:
        return recorder.exit_code
    # Equal-in-readable-parts is stated, not failed: it is not a proven change.
    if args.fail_on_change and (
        comparison_unknown or stream_changes or any(change.kind != EQUAL_READABLE for change in changes)
    ):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
