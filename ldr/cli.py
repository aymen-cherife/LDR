"""Agent-first command line interface for recording and behavioural diffing."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import runpy
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any

from .diff import align, diff_tree, mark_unstable, rank
from .node import Node
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
from .tracer import Recorder

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
    **kwargs,
):
    recorder = Recorder(scope=scope, **kwargs)
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
            recorder.crash_summary = f"{type(exc).__name__}: {exc}"
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
    current = os.path.realpath(start)
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
            return os.path.realpath(start)
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
        "arguments": list(args.rest),
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


def _finding_payload(changes) -> list[dict[str, Any]]:
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


def _write_result(text: str, payload: dict[str, Any], ldr_dir: str = LDR_DIR) -> tuple[str, str]:
    os.makedirs(ldr_dir, exist_ok=True)
    text_path = os.path.join(ldr_dir, "last.txt")
    json_path = os.path.join(ldr_dir, "last.json")
    atomic_text(text_path, text)
    atomic_json(json_path, payload)
    return text_path, json_path


def _probe(
    args,
    scope: list[str],
    invocation: dict[str, Any],
    recorder: Recorder,
    ldr_dir: str,
) -> list[Node]:
    os.makedirs(ldr_dir, exist_ok=True)
    fd, probe_path = tempfile.mkstemp(prefix="probe-", suffix=".json", dir=ldr_dir)
    os.close(fd)
    os.unlink(probe_path)
    environment = dict(os.environ, LDR_PROBE=probe_path)
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
        raise RuntimeError("volatility probe timed out; snapshot was not saved") from exc
    try:
        if not os.path.exists(probe_path):
            detail = completed.stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"volatility probe produced no trace: {detail}")
        roots, envelope = load_trace(probe_path)
        probe_status = envelope.get("status", {})
        if probe_status.get("exit_code") != recorder.exit_code:
            raise RuntimeError(
                "volatility probe changed runner status; snapshot was not saved "
                f"({recorder.exit_code} then {probe_status.get('exit_code')})"
            )
        return roots
    finally:
        try:
            os.unlink(probe_path)
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    total_started = time.perf_counter()
    initial_cwd = os.path.realpath(os.getcwd())
    ldr_dir = os.path.join(initial_cwd, LDR_DIR)
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, passthrough = argv[:index], argv[index + 1 :]

    parser = argparse.ArgumentParser(prog="ldr")
    parser.add_argument("--version", action="version", version="ldr 0.5.1")
    parser.add_argument("mode", choices=["run", "pytest", "module", "verify", "inspect"])
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

    def go(recorder):
        if args.mode == "run":
            sys.argv = [args.target] + args.rest
            runpy.run_path(args.target, run_name="__main__")
        elif args.mode == "module":
            sys.argv = [args.target] + args.rest
            runpy.run_module(args.target, run_name="__main__", alter_sys=True)
        else:
            import pytest

            class RuntimeOnlyPlugin:
                """Arm tracing after collection, before the selected test's setup."""

                def pytest_runtest_protocol(self, item, nextitem):
                    if not recorder._active:
                        recorder.start()

                def pytest_runtest_makereport(self, item, call):
                    if call.excinfo is not None:
                        recorder.runner_exception = call.excinfo.value

            code = pytest.main(
                [args.target, *args.rest, "-q", "-p", "no:cacheprovider"],
                plugins=[RuntimeOnlyPlugin()],
            )
            if int(code) != 0:
                raise SystemExit(int(code))

    execution_started = time.perf_counter()
    try:
        recorder = _record(
            go,
            scope,
            timeout=args.timeout,
            defer_start=args.mode == "pytest",
            max_nodes=args.max_nodes,
            project_root=project_root,
            display_root=initial_cwd,
            focus_frame=args.frame if requested_mode == "inspect" else None,
            max_state_steps=args.max_state_steps,
            max_calls_per_function=args.max_calls_per_function,
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

    status = _status(recorder)
    status["timing"] = {
        "execution_seconds": round(execution_finished - execution_started, 6),
    }
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
    probe_path = os.environ.get("LDR_PROBE")
    if probe_path:
        save_trace(probe_path, recorder.roots, invocation=invocation, status=status)
        return recorder.exit_code

    if requested_mode == "inspect":
        inspected = [
            node
            for root in recorder.roots
            for node in root.walk()
            if (
                node.frame_id.startswith(args.frame[:-1])
                if args.frame.endswith("#*")
                else node.frame_id == args.frame
            )
        ]
        frame_index_payload = _frame_index(recorder.roots)
        display_scope = _display_scope(scope, initial_cwd)
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
        )
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
                "frames": [node.to_dict() for node in inspected],
            },
            "frame_index": frame_index_payload,
            "findings": [],
            "failures": _failure_payload(recorder.roots),
        }
        text_path, json_path = _write_result(text, payload, ldr_dir)
        print(text)
        print(
            f"\n[ldr] agent result: {os.path.relpath(json_path, initial_cwd)}; "
            f"text: {os.path.relpath(text_path, initial_cwd)}",
            file=sys.stderr,
        )
        if recorder.exit_code != 0:
            return recorder.exit_code
        return 0 if inspected else 2

    changes = []
    snapshot_action = "none"
    frame_index_payload = _frame_index(recorder.roots)
    suggestions = rank_inspections(recorder.roots, frame_index_payload)
    display_scope = _display_scope(scope, initial_cwd)
    rerun = _rerun_payload(args, scope, initial_cwd, project_root)
    try:
        if args.golden:
            if args.learn_volatility:
                second = _probe(args, scope, invocation, recorder, ldr_dir)
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
            if args.learn_volatility:
                text += f"\n{volatility} node(s) contain field-level volatility masks."
        elif args.diff:
            if not os.path.exists(golden_path):
                raise FileNotFoundError(
                    f"No snapshot for this invocation ({key}). Run verify or --golden first."
                )
            old, envelope = load_trace(golden_path)
            if envelope.get("invocation") not in ({}, invocation):
                raise ValueError("snapshot invocation metadata does not match this command")
            for left, right in align(old, recorder.roots):
                changes.extend(diff_tree(left, right))
            snapshot_action = "compared"
            if changes:
                text = "BEST GUESS, RANKED\n" + render_diff(changes, args.color)
                text += "\n\nCALL CHAIN\n" + render_call_chain(recorder.roots, changes, args.color)
            else:
                text = "No behavioural change vs snapshot."
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
            "changed": bool(changes),
            "finding_count": len(changes),
        },
        "frame_index": frame_index_payload,
        "suggestions": suggestions,
        "scope": {
            "detected_production_roots": _display_scope(detected_roots, initial_cwd),
            "warnings": scope_warnings,
        },
        "findings": _finding_payload(changes),
        "failures": _failure_payload(recorder.roots),
    }
    text_path, json_path = _write_result(text, payload, ldr_dir)
    print(text)
    print(
        f"\n[ldr] agent result: {os.path.relpath(json_path, initial_cwd)}; "
        f"text: {os.path.relpath(text_path, initial_cwd)}",
        file=sys.stderr,
    )

    if recorder.exit_code != 0:
        return recorder.exit_code
    if args.fail_on_change and changes:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
