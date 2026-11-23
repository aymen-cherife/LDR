"""The waiting command: it starts a run, shows its output as it happens, and exits with the
program's exit status. It only watches; the run belongs to the supervisor (supervisor.py),
so killing this process does not stop the run.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from uuid import uuid4

from .supervisor import RUNS, alive, read_state, run_directory, spawn_detached

PROGRESS_EVERY = 10.0  # seconds between progress lines when the output is not a terminal
POLL = 0.05


def _ensure_ldr_dir(ldr_dir: str) -> None:
    os.makedirs(ldr_dir, mode=0o700, exist_ok=True)
    try:
        with open(os.path.join(ldr_dir, ".gitignore"), "x", encoding="utf-8") as handle:
            handle.write("*\n")
    except FileExistsError:
        pass


def describe_progress(state: dict) -> str:
    """One compact line: calls recorded, tests started, elapsed time, slowdown so far."""
    elapsed = state.get("elapsed") or 0
    parts = [f"{state.get('calls', 0):,} call(s) recorded"]
    if state.get("tests"):
        parts.append(f"{state['tests']} test(s) started")
    parts.append(f"elapsed {elapsed:.0f}s")
    baseline = state.get("baseline_seconds")
    if baseline:
        parts.append(
            f"at least {elapsed / baseline:.1f}x the untraced time ({baseline:.1f}s)"
            if elapsed > baseline else f"still within the untraced time ({baseline:.1f}s)"
        )
    else:
        parts.append("slowdown: no baseline")
    return f"[ldr] running {state.get('run', '')}: " + ", ".join(parts)


class _Tail:
    def __init__(self, path: str, out):
        self.path, self.out, self.offset = path, out, 0

    def drain(self) -> None:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return
        if data:
            self.offset += len(data)
            try:
                self.out.buffer.write(data)
                self.out.flush()
            except (OSError, ValueError, AttributeError):
                pass


def _forward_stdin(connection) -> None:
    try:
        stream = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        stream = None
    try:
        while stream is not None:
            data = os.read(stream, 65536)
            if not data:
                break
            connection.send(("stdin", data))
    except (OSError, EOFError, ValueError):
        pass
    try:
        connection.send(("eof",))
    except (OSError, EOFError, ValueError):
        pass


def run(argv: list[str], project_root: str) -> int:
    cwd = os.getcwd()
    ldr_dir = os.path.join(cwd, ".ldr")
    _ensure_ldr_dir(ldr_dir)
    run_id = uuid4().hex[:12]
    rundir = run_directory(ldr_dir, run_id)
    os.makedirs(rundir, exist_ok=True)
    started = time.time()
    spawn_detached(rundir, {"argv": argv, "project_root": project_root, "cwd": cwd,
                            "env": dict(os.environ), "started": started})

    # The supervisor says where to send stdin once the program is running.
    deadline = time.time() + 120
    state = None
    while time.time() < deadline:
        state = read_state(rundir)
        if state and (state.get("control") or state.get("state") == "finished"):
            break
        time.sleep(POLL)
    else:
        print(f"LDR: the run {run_id} did not start in 120 s (see {rundir})", file=sys.stderr)
        return 2

    connection = None
    if state.get("control"):
        try:
            from multiprocessing.connection import Client

            address, key = state["control"].rsplit("|", 1)
            connection = Client(_address(address), authkey=bytes.fromhex(key))
            threading.Thread(target=_forward_stdin, args=(connection,), daemon=True).start()
        except Exception:
            connection = None  # the program has no stdin

    out, err = _Tail(os.path.join(rundir, "stdout.log"), sys.stdout), _Tail(os.path.join(rundir, "stderr.log"), sys.stderr)
    interactive = bool(getattr(sys.stderr, "isatty", lambda: False)())
    next_progress = time.time() + PROGRESS_EVERY
    gone = 0
    try:
        while True:
            out.drain()
            err.drain()
            state = read_state(rundir) or {}
            if state.get("state") == "finished":
                out.drain()
                err.drain()
                break
            # A supervisor that is gone without a result must not leave this waiting for ever.
            gone = gone + 1 if state.get("pid") and not alive(state["pid"]) else 0
            if gone > 100:  # about five seconds
                out.drain()
                err.drain()
                print(f"LDR: the supervisor of run {run_id} is gone without a result (see {rundir})", file=sys.stderr)
                return 2
            if not interactive and time.time() >= next_progress:
                next_progress = time.time() + PROGRESS_EVERY
                print(describe_progress(state), file=sys.stderr, flush=True)
            time.sleep(POLL)
    except KeyboardInterrupt:
        print(
            f"\nLDR: stopped waiting. The run continues; its result: ldr status {run_id}",
            file=sys.stderr,
        )
        return 130
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
    if state.get("message"):
        print(state["message"], file=sys.stderr)
    return int(state.get("exit_code", 2))


def _address(text: str):
    """A listener address as text back to what Client takes (a path, or host and port)."""
    if text.startswith("(") and text.endswith(")"):
        host, port = text[1:-1].split(",")
        return host.strip().strip("'\""), int(port)
    return text
