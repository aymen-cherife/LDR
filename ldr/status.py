"""``ldr status [RUN_ID]``: where a run is, or how it ended.

Reads the run's directory (``.ldr/runs/<run id>/``); it never runs anything. While the run
is going it shows the progress line; once it ended, the exit code and the end of its output.
"""

from __future__ import annotations

import os
import sys
import time

from .supervisor import RUNS, read_state

TAIL_LINES = 15


def _find(ldr_dir: str, wanted: str | None) -> str | None:
    directory = os.path.join(ldr_dir, RUNS)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    ordered = sorted(names, key=lambda name: os.path.getmtime(os.path.join(directory, name)), reverse=True)
    if wanted is None:
        return os.path.join(directory, ordered[0]) if ordered else None
    for name in ordered:
        state = read_state(os.path.join(directory, name)) or {}
        if name.startswith(wanted) or (state.get("store_run_id") or "").startswith(wanted):
            return os.path.join(directory, name)
    return None


def _tail(path: str, lines: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:]


def render(directory: str) -> str:
    from .waiter import describe_progress

    state = read_state(directory) or {}
    run = state.get("run") or os.path.basename(directory)
    lines = []
    if state.get("state") != "finished":
        lines.append(f"RUN {run}: {state.get('state', 'unknown')}")
        if state.get("state") == "running":
            lines.append(describe_progress(state))
            if state.get("started_at"):
                lines.append(f"started {time.time() - state['started_at']:.0f}s ago")
    else:
        spent = (state.get("ended_at") or 0) - (state.get("started_at") or 0)
        lines.append(f"RUN {run}: finished in {spent:.1f}s, exit code {state.get('exit_code')}")
        if state.get("store_run_id"):
            lines.append(f"stored run: {state['store_run_id']}  (ldr show --run-id {state['store_run_id']})")
        if state.get("message"):
            lines.append(state["message"])
        lines.append("result: .ldr/last.txt and .ldr/last.json")
    for name, label in (("stdout.log", "output"), ("stderr.log", "errors")):
        tail = _tail(os.path.join(directory, name), TAIL_LINES)
        if tail:
            lines.append(f"{'end of ' if state.get('state') == 'finished' else 'latest '}{label}:")
            lines.extend(f"  {line}" for line in tail)
    return "\n".join(lines)


def main(argv, ldr_dir: str | None = None) -> int:
    ldr_dir = ldr_dir or os.path.join(os.getcwd(), ".ldr")
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        print("usage: ldr status [RUN_ID]", file=sys.stderr)
        return 2
    directory = _find(ldr_dir, argv[0] if argv else None)
    if directory is None:
        print("No such run." if argv else "No runs recorded here.", file=sys.stderr)
        return 2
    print(render(directory))
    return 0
