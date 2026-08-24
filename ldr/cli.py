"""Agent-first command line interface for recording and behavioural diffing."""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import runpy
import signal
import subprocess
import sys
import tempfile
from typing import Any

from .diff import align, diff_tree, mark_unstable, rank
from .node import Node
from .render import render_call_chain, render_diff, render_run
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
from .tracer import Recorder

LDR_DIR = ".ldr"
_KNOWN_RUNNERS = {"unittest", "pytest", "doctest", "pdb", "cProfile", "trace", "timeit"}
_PROJECT_MARKERS = (".git", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini")


class _RunTimedOut(BaseException):
    pass


def _record(fn, scope: list[str], *, timeout: float = 300, **kwargs):
    recorder = Recorder(scope=scope, **kwargs)
    recorder.crashed = False
    recorder.exit_code = 0
    recorder.crash_summary = ""
    recorder.escaped_exception = None
    previous_handler = None
    alarm_enabled = timeout > 0 and hasattr(signal, "SIGALRM")

    def expired(signum, frame):
        raise _RunTimedOut(f"execution exceeded {timeout:g} seconds")

    recorder.start()
    try:
        if alarm_enabled:
            previous_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            fn()
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


def _status(recorder: Recorder) -> dict[str, Any]:
    return {
        "ok": recorder.exit_code == 0 and not recorder.crashed,
        "exit_code": recorder.exit_code,
        "crashed": recorder.crashed,
        "crash_summary": recorder.crash_summary,
        "trace_truncated": recorder.truncated,
        "recorded_nodes": recorder.n_nodes,
        "overflow_transitions": recorder.dropped_nodes,
    }


def _node_state(node: Node | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
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

    def visit(node: Node, path: list[str]):
        here = path + [node.name]
        if node.error is not None and not node.handled:
            failures.append(
                {
                    "path": here,
                    "function": node.name,
                    "source_id": node.source_id,
                    "location": node.location,
                    "callsite": node.callsite,
                    "error": node.error,
                    "inputs": node.in_,
                    "recent_calls": _recent_context(node),
                }
            )
        for child in node.children:
            visit(child, here)

    for root in roots:
        visit(root, [])
    return failures


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
    initial_cwd = os.path.realpath(os.getcwd())
    ldr_dir = os.path.join(initial_cwd, LDR_DIR)
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, passthrough = argv[:index], argv[index + 1 :]

    parser = argparse.ArgumentParser(prog="ldr")
    parser.add_argument("--version", action="version", version="ldr 0.4.1")
    parser.add_argument("mode", choices=["run", "pytest", "module", "verify"])
    parser.add_argument("target")
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
    parser.add_argument("--max-lines", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=300)
    args, unknown = parser.parse_known_args(argv)
    args.rest = list(args.rest) + unknown + passthrough
    if args.golden and args.diff:
        parser.error("--golden and --diff are mutually exclusive")
    if args.max_lines < 20:
        parser.error("--max-lines must be at least 20")
    if args.max_nodes < 1:
        parser.error("--max-nodes must be at least 1")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        parser.error("--timeout must be a finite non-negative number")

    requested_mode = args.mode
    if requested_mode == "verify":
        args.mode = _infer_runner(args.target) if args.runner == "auto" else args.runner
    elif args.runner != "auto":
        parser.error("--runner is only valid with verify")

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

    scope = list(args.only) if args.only else [project_root]
    for directory in _entry_dirs(args.mode, args.target, args.rest):
        if directory not in scope:
            scope.append(directory)
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

    def go():
        if args.mode == "run":
            sys.argv = [args.target] + args.rest
            runpy.run_path(args.target, run_name="__main__")
        elif args.mode == "module":
            sys.argv = [args.target] + args.rest
            runpy.run_module(args.target, run_name="__main__", alter_sys=True)
        else:
            import pytest

            code = pytest.main([args.target, *args.rest, "-q", "-p", "no:cacheprovider"])
            if int(code) != 0:
                raise SystemExit(int(code))

    try:
        recorder = _record(
            go,
            scope,
            timeout=args.timeout,
            max_nodes=args.max_nodes,
            project_root=project_root,
            display_root=initial_cwd,
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

    status = _status(recorder)
    probe_path = os.environ.get("LDR_PROBE")
    if probe_path:
        save_trace(probe_path, recorder.roots, invocation=invocation, status=status)
        return recorder.exit_code

    changes = []
    snapshot_action = "none"
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
    text = _limit_text(note + text, args.max_lines)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "invocation": invocation,
        "status": status,
        "snapshot": {"key": key, "path": golden_path, "action": snapshot_action},
        "behavior": {
            "changed": bool(changes),
            "finding_count": len(changes),
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
