"""Measure one first run and one successive run per checkout and fixture.

The normal run starts its own machine check beside the launcher. Read its
facts from the result after the command has returned.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import overhead
from ldr import processes

HERE = Path(__file__).resolve().parent
STEPS = (
    "call_records_move_with_indexes", "fingerprint_lookup_move_with_index",
    "report_ordered_scan", "report_grouped_counts",
    "result_serialization_and_write", "output_drain",
)


def one(root: Path, command: list[str], env: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="ldr-phases-") as scratch:
        instrumented = dict(env, LDR_PHASE_OUTPUT=scratch)
        instrumented["PYTHONPATH"] += os.pathsep + str(HERE / "phase_probe")
        overhead_started = time.time()
        started = time.perf_counter()
        result = processes.run(command, cwd=root, env=instrumented, capture_output=True, text=True)
        wall = time.perf_counter() - started
        returned = time.time()
        if result.returncode:
            raise RuntimeError(f"recording exited {result.returncode}\n{result.stdout}\n{result.stderr}")
        path = Path(scratch)
        program, = [json.loads(item.read_text(encoding="utf-8")) for item in path.glob("program-*.json")]
        supervisor, = [json.loads(item.read_text(encoding="utf-8")) for item in path.glob("supervisor-*.json")]

    ldr = root / ".ldr"
    status = json.loads((ldr / "last.json").read_text(encoding="utf-8"))["status"]
    states = [json.loads(path.read_text(encoding="utf-8")) for path in (ldr / "runs").glob("*/state.json")]
    state, = [row for row in states if row.get("store_run_id") == status["run_id"]]
    timing = status["timing"]
    program_end = timing["execution_finished_at"]
    first = state["started_at"] + state["first_call_s"]
    measured = program["seconds"] | supervisor["seconds"]
    post_run = {step: measured[step] for step in STEPS}
    post_run["comparison_temp_index"] = measured.get("comparison_temp_index", 0.0)
    post_run["comparison"] = timing.get("comparison_seconds", 0.0) - post_run["comparison_temp_index"]
    post_run["untimed"] = state["child_ended_at"] - program_end - sum(post_run.values())
    return {
        "wall": round(wall, 3),
        "python_start": round(state["started_at"] - overhead_started, 3),
        "to_first_committed_batch": state["first_call_s"],
        "program_after_first_batch": round(program_end - first, 3),
        "post_run_seconds": {key: round(value, 3) for key, value in post_run.items()},
        "pruning": round(state["pruned_at"] - state["child_ended_at"], 3),
        "return": round(returned - state["pruned_at"], 3),
        "peak_rss_bytes": program["peak_rss_bytes"],
        "supervisor_peak_rss_bytes": supervisor["peak_rss_bytes"],
        "python_version": program["python"],
        "machine": status["machine"]["summary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", action="append", required=True, metavar="LABEL=PATH")
    parser.add_argument("--only", nargs="+", choices=overhead.NAMES, default=["rich"])
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
    args = parser.parse_args()
    work = Path(args.work)
    for name in args.only:
        root, _, traced = overhead.build(work, name)
        for checkout in args.checkout:
            label, path = checkout.split("=", 1)
            path = Path(path).resolve()
            commit = processes.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
            env = overhead.environment(path, root)
            overhead.clean(root)
            for run in ("first", "second"):
                row = one(root, traced, env)
                print(json.dumps({
                    "checkout": label, "commit": commit, "fixture": name, "run": run,
                    "platform": platform.platform(), "mode": "default", **row,
                }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
