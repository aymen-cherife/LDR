"""The detached process that owns a run (close-list item 6).

A recording run has three roles. The *waiting command* (``waiter.py``) is what the user or
an agent starts; it only watches. The *supervisor* (this module) writes the records, starts
the program, keeps its output in the run's log and its state in ``state.json``, and marks a
run that died. The *program* is the child that runs the user's code. The supervisor and the
program keep going if the waiting command is killed, for example by a shell's time limit.

The run's directory is ``.ldr/runs/<run id>/``: ``state.json`` (progress, then the result),
``stdout.log`` and ``stderr.log``. What to run (the command line and the environment, which
may hold credentials) is handed to the supervisor through a pipe and never written to disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from . import processes

RUNS = "runs"
KEEP_RUNS = 50


def run_directory(ldr_dir: str, run_id: str) -> str:
    return os.path.join(ldr_dir, RUNS, run_id)


class State:
    """``state.json`` of one run: the supervisor writes it, anyone may read it."""

    def __init__(self, directory: str):
        self.path = os.path.join(directory, "state.json")
        self.fields: dict = {}
        self.lock = threading.Lock()

    def update(self, **fields) -> None:
        with self.lock:
            if self.fields.get("state") == "finished":
                fields.pop("state", None)  # a late progress write must not reopen a finished run
            self.fields.update(fields)
            temporary = f"{self.path}.{os.getpid()}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.fields, handle)
            # Windows refuses to replace a file that a reader has open at that instant.
            for attempt in range(200):
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError:
                    if attempt == 199:
                        raise
                    time.sleep(0.01)


def alive(pid: int) -> bool:
    """Whether a process exists. (os.kill with signal 0 would terminate it on Windows.)"""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state(directory: str) -> dict | None:
    try:
        with open(os.path.join(directory, "state.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def spawn_detached(rundir: str, spec: dict) -> None:
    """Start the supervisor so that killing this process, or its whole tree, does not stop it.

    The waiting command starts a short-lived launcher, which starts the real supervisor
    detached and exits at once: the supervisor then has no living parent to share a fate with.
    ``spec`` (what to run) travels through the launcher's stdin to the supervisor's stdin.
    """
    command = [sys.executable, "-m", "ldr.supervisor", "--detach", rundir]
    processes.run(
        command, input=json.dumps(spec).encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def _start_real(rundir: str, spec: bytes) -> None:
    command = [sys.executable, "-m", "ldr.supervisor", "--run", rundir]
    options = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        # CREATE_NO_WINDOW (added by the helper) replaces DETACHED_PROCESS: the two cannot be combined.
        group, breakaway = 0x00000200, 0x01000000
        try:
            process = processes.Popen(command, creationflags=group | breakaway, **options)
        except OSError:  # the job this runs in does not allow breaking away
            process = processes.Popen(command, creationflags=group, **options)
    else:
        process = processes.Popen(command, start_new_session=True, **options)
    process.stdin.write(spec)
    process.stdin.close()


def _pump(stream, path: str) -> None:
    with open(path, "ab", buffering=0) as log:
        while True:
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                return
            log.write(chunk)


def _control(listener, process) -> None:
    """The waiting command's connection: its stdin goes to the program. When it drops, stdin ends."""
    try:
        connection = listener.accept()
    except Exception:
        return
    try:
        while True:
            message = connection.recv()
            if message[0] == "stdin":
                try:
                    process.stdin.write(message[1])
                    process.stdin.flush()
                except (OSError, ValueError):
                    return
            else:
                break
    except (EOFError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass


def supervise(rundir: str) -> int:
    from multiprocessing.connection import Listener

    from .interpreter import run_in_child

    spec = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    os.chdir(spec["cwd"])
    os.environ.clear()
    os.environ.update(spec["env"])
    ldr_dir = os.path.join(spec["cwd"], ".ldr")
    state = State(rundir)
    started = time.time()
    from .machine import process_identity

    identity = process_identity(os.getpid())
    state.update(state="starting", pid=os.getpid(), pid_start=identity[0] if identity else None,
                 started_at=spec["started"], run=os.path.basename(rundir))
    said: list[str] = []
    pumps: list[threading.Thread] = []
    holder: dict = {}

    def on_start(process, sink):
        holder["sink"], holder["process"] = sink, process
        key = os.urandom(16)
        listener = Listener(authkey=key)
        threading.Thread(target=_control, args=(listener, process), daemon=True).start()
        for name, stream in (("stdout.log", process.stdout), ("stderr.log", process.stderr)):
            thread = threading.Thread(target=_pump, args=(stream, os.path.join(rundir, name)), daemon=True)
            thread.start()
            pumps.append(thread)
        try:
            state.update(state="running", program_pid=process.pid, control=f"{listener.address}|{key.hex()}")
        except OSError:
            pass  # the progress thread writes the state again; the run itself must not fail over it
        threading.Thread(target=_progress, args=(state, sink, ldr_dir, started), daemon=True).start()
        threading.Thread(target=_first_call, args=(state, sink, spec["started"]), daemon=True).start()

    code = 2
    try:
        code = run_in_child(
            spec["argv"], spec["project_root"],
            popen_kwargs={"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE},
            started=on_start, say=said.append,
        )
    except Exception as exc:  # the supervisor must always leave a result
        said.append(f"LDR could not supervise the run: {type(exc).__name__}: {exc}")
    for thread in pumps:
        thread.join(10)
    child_ended_at = time.time()
    sink = holder.get("sink")
    run_id = None
    if sink is not None and sink.main_run:
        run_id = sink.main_run
    try:  # only the last runs of each target are kept (before the run is announced as finished)
        from .previous import keep_runs, prune as prune_runs

        prune_runs(ldr_dir, keep_runs(spec["project_root"]))
    except Exception as exc:
        said.append(f"LDR could not delete old runs: {type(exc).__name__}: {exc}")
    try:  # LDR's own leftovers: runs whose supervisor died (see machine.py); a live run is never touched
        from .machine import Report, clean_dead, scan_runs

        fixed = Report()
        clean_dead(ldr_dir, scan_runs(ldr_dir), fixed, rows=True)
        said.extend(f"LDR fixed: {fix}" for fix in fixed.fixes)
    except Exception as exc:
        said.append(f"LDR could not clean dead runs: {type(exc).__name__}: {exc}")
    state.update(
        state="finished", exit_code=code, message="\n".join(said) or None, ended_at=time.time(),
        store_run_id=run_id, child_ended_at=child_ended_at, pruned_at=time.time(),
    )
    prune(ldr_dir)
    return code


def _first_call(state: State, sink, launched: float) -> None:
    """The Python start time of this run: from the launch to the first recorded call."""
    while state.fields.get("state") == "running":
        run = sink.runs.get(sink.main_run) if sink.main_run else None
        if run is not None:
            with run.lock:
                calls = sum(worker["fingerprinted"] for worker in run.counts.values())
            if calls:
                try:
                    state.update(first_call_s=round(time.time() - launched, 2))
                except OSError:
                    pass
                return
        time.sleep(0.02)


def _progress(state: State, sink, ldr_dir: str, started: float) -> None:
    """Keep progress current while the program runs: calls recorded, tests started, elapsed."""
    while state.fields.get("state") == "running":
        calls = tests = 0
        run = sink.runs.get(sink.main_run) if sink.main_run else None
        if run is not None:
            with run.lock:
                calls = sum(worker["fingerprinted"] for worker in run.counts.values())
                tests = run.tests
        try:
            state.update(calls=calls, tests=tests, elapsed=round(time.time() - started, 1),
                         store_run_id=sink.main_run, state="running")
        except OSError:
            pass
        time.sleep(1.0)


def prune(ldr_dir: str, keep: int = KEEP_RUNS) -> None:
    directory = os.path.join(ldr_dir, RUNS)
    try:
        runs = sorted((os.path.getmtime(os.path.join(directory, name)), name) for name in os.listdir(directory))
    except OSError:
        return
    import shutil

    for _, name in runs[:-keep]:
        shutil.rmtree(os.path.join(directory, name), ignore_errors=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--detach":
        _start_real(argv[1], sys.stdin.buffer.read())
        return 0
    if len(argv) == 2 and argv[0] == "--run":
        return supervise(argv[1])
    print("usage: python -m ldr.supervisor --detach|--run RUN_DIRECTORY", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
