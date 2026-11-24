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

from .supervisor import read_state, run_directory, spawn_detached

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
    """One compact line: calls recorded, tests started, elapsed time."""
    elapsed = state.get("elapsed") or 0
    parts = [f"{state.get('calls', 0):,} call(s) recorded"]
    if state.get("tests"):
        parts.append(f"{state['tests']} test(s) started")
    parts.append(f"elapsed {elapsed:.0f}s")
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


def _supervisor_alive(state: dict) -> bool:
    """Whether the process named in the state is the supervisor (its start time and name are checked,
    because a number can be given to another process once the supervisor is gone)."""
    from .machine import supervisor_alive

    return bool(state.get("pid")) and supervisor_alive(state)


def run(argv: list[str], project_root: str) -> int:
    cwd = os.getcwd()
    ldr_dir = os.path.join(cwd, ".ldr")
    _ensure_ldr_dir(ldr_dir)
    run_id = uuid4().hex[:12]
    rundir = run_directory(ldr_dir, run_id)
    os.makedirs(rundir, exist_ok=True)
    started = time.time()
    from . import machine
    from .previous import keep_runs

    check = machine.Background(ldr_dir, keep_runs(project_root))  # beside the run's start: nothing waits for it
    launch = spawn_detached(rundir, {"argv": argv, "project_root": project_root, "cwd": cwd,
                                     "env": dict(os.environ), "started": started})
    try:
        identity = json.loads(launch.stdout or b"{}")
    except (ValueError, UnicodeError):
        identity = {}
    if launch.returncode != 0 or not identity.get("pid"):
        reason = f"exit {launch.returncode}" if launch.returncode != 0 else "it wrote no process number"
        detail = (launch.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        print(f"LDR: the launcher of run {run_id} failed ({reason}{': ' + detail[0] if detail else ''}); the run did not start (see {rundir})",
              file=sys.stderr)
        return 2

    state = {"state": "starting", **identity}
    out, err = _Tail(os.path.join(rundir, "stdout.log"), sys.stdout), _Tail(os.path.join(rundir, "stderr.log"), sys.stderr)
    interactive = bool(getattr(sys.stderr, "isatty", lambda: False)())
    next_progress = time.time() + PROGRESS_EVERY
    connection = None
    try:
        # No time limit: the run waits as long as the supervisor is alive, however slow the start.
        while True:
            out.drain()
            err.drain()
            state = read_state(rundir) or state
            if connection is None and state.get("control"):
                # The supervisor says where to send stdin once the program is running.
                try:
                    from multiprocessing.connection import Client

                    address, key = state["control"].rsplit("|", 1)
                    connection = Client(_address(address), authkey=bytes.fromhex(key))
                    threading.Thread(target=_forward_stdin, args=(connection,), daemon=True).start()
                except Exception:
                    connection = False  # the program has no stdin
            if check.ready() and not check.printed and state.get("first_call_s") is not None:
                check.print_once(state)
            if state.get("state") == "finished":
                break
            if not _supervisor_alive(state):
                state = read_state(rundir) or state  # the last word, in case it was written as it exited
                out.drain()
                err.drain()
                if state.get("state") == "finished":
                    break
                print(f"LDR: the supervisor of run {run_id} is gone without a result (see {rundir})", file=sys.stderr)
                break
            if not interactive and time.time() >= next_progress:
                next_progress = time.time() + PROGRESS_EVERY
                print(describe_progress(state), file=sys.stderr, flush=True)
            time.sleep(POLL)
        out.drain()
        err.drain()
    except KeyboardInterrupt:
        print(
            f"\nLDR: stopped waiting. The run continues; its result: ldr status {run_id}",
            file=sys.stderr,
        )
        return 130
    finally:
        if connection:
            try:
                connection.close()
            except OSError:
                pass
    if state.get("message"):
        print(state["message"], file=sys.stderr)
    report = check.finish(state)
    check.print_once(state)
    if report is not None:
        machine.save(ldr_dir, report, merge_after=started)
    return int(state.get("exit_code", 2))


def _address(text: str):
    """A listener address as text back to what Client takes (a path, or host and port)."""
    if text.startswith("(") and text.endswith(")"):
        host, port = text[1:-1].split(",")
        return host.strip().strip("'\""), int(port)
    return text
