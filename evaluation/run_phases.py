"""A run from the command's start to its return, split by phase, for two or more checkouts of LDR.

    python evaluation/run_phases.py --checkout LABEL=PATH --checkout LABEL=PATH [--repeats 3]
                                    [--only orders toolz rich] [--work DIR] [--json]

For each fixture, repeat and checkout: remove .ldr, run (the first run), run again (the second run,
compared with the first), and record both. `rich` records both runs; the others only the second.
Before each series `ldr check_machine` runs, and the series does not start on a machine it flags
(it waits up to --wait seconds for it to clear, then stops).

Phases (seconds), from the clock times the run leaves in .ldr/runs/<id>/state.json and last.json:
  python    the command's start to the waiting command's own start (interpreter, imports)
  start     that to the first recorded call
  program   the first call to the end of the program
  compare   the comparison with the previous run (after the program, in the program's process)
  writing   the rest of the work after the program: the result and what it stores
  pruning   deleting old runs, cleaning, announcing the end (the supervisor)
  return    the supervisor's last write to the command's return
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overhead  # noqa: E402


def check_machine(checkout: Path, root: Path, env: dict) -> str:
    result = subprocess.run([sys.executable, "-m", "ldr", "check_machine"], cwd=root, env=env, capture_output=True, text=True)
    return result.stdout.strip()


def wait_for_a_clean_machine(checkout: Path, root: Path, env: dict, seconds: int) -> str:
    deadline = time.time() + seconds
    while True:
        text = check_machine(checkout, root, env)
        if text.splitlines()[0].startswith("machine: ok"):
            return text
        if time.time() > deadline:
            raise SystemExit("the machine is flagged; not timing:\n" + text)
        time.sleep(20)


def one(root: Path, command: list[str], env: dict) -> dict:
    overhead_started = time.time()
    started = time.perf_counter()
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    wall = time.perf_counter() - started
    returned = time.time()
    if result.returncode != 0:
        raise SystemExit(f"failed: {command}\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
    states = sorted((root / ".ldr" / "runs").glob("*/state.json"), key=lambda path: path.stat().st_mtime)
    state = json.loads(states[-1].read_text(encoding="utf-8"))
    timing = json.loads((root / ".ldr" / "last.json").read_text(encoding="utf-8"))["status"]["timing"]
    first = state["started_at"] + state["first_call_s"]
    program_ended = timing["execution_finished_at"]
    phases = {
        "python": state["started_at"] - overhead_started,
        "start": state["first_call_s"],
        "program": program_ended - first,
        "compare": timing.get("comparison_seconds", 0.0),
        "writing": state["child_ended_at"] - program_ended - timing.get("comparison_seconds", 0.0),
        "pruning": state["pruned_at"] - state["child_ended_at"],
        "return": returned - state["pruned_at"],
    }
    return {"wall": round(wall, 3), **{key: round(value, 3) for key, value in phases.items()}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", action="append", required=True, metavar="LABEL=PATH")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", nargs="+", default=["orders", "toolz", "rich"])
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
    parser.add_argument("--wait", type=int, default=600)
    args = parser.parse_args()
    checkouts = dict(item.split("=", 1) for item in args.checkout)
    work = Path(args.work)
    for name in args.only:
        root, _, traced = overhead.build(work, name)
        results = {label: {"first": [], "second": []} for label in checkouts}
        for repeat in range(args.repeats):
            for label, checkout in checkouts.items():
                env = overhead.environment(Path(checkout), root)
                summary = wait_for_a_clean_machine(Path(checkout), root, env, args.wait)
                overhead.clean(root)
                first = one(root, traced, env)
                second = one(root, traced, env)
                results[label]["first"].append(first)
                results[label]["second"].append(second)
                print(json.dumps({"fixture": name, "repeat": repeat, "checkout": label, "check": summary.splitlines()[0][:160],
                                  "first": first, "second": second}), flush=True)
        medians = {
            label: {run: {key: round(statistics.median(item[key] for item in rows), 3) for key in rows[0]}
                    for run, rows in runs.items()}
            for label, runs in results.items()
        }
        print(json.dumps({"fixture": name, "medians": medians}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
