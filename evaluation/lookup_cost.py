"""What the per-call lookup of the previous run costs: two checkouts of LDR, same session.

    python evaluation/lookup_cost.py --checkout LABEL=CHECKOUT --checkout LABEL=CHECKOUT ... [--rounds 3]
                                     [--work DIR] [--only NAME ...] [--json]

The first checkout is the reference; every other is reported against it.

Per round and fixture: one plain run, then for each checkout one traced run that is not timed
(it leaves a previous run behind, as the second run of any target has) and one traced run that
is. So the timed runs of both checkouts compare with a previous run of the same target.
Plain and traced runs are interleaved; the fixtures are those of overhead.py. One more run of
each checkout is timed from inside by evaluation/lookup_probe: the time to load the previous run
into memory (the checkout before the lookup was added) and the time in the comparison of each call.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import statistics
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overhead  # noqa: E402


def calls_of(root: Path) -> int:
    with closing(sqlite3.connect(f"file:{root / '.ldr' / 'traces.sqlite3'}?mode=ro", uri=True)) as connection:
        run = connection.execute("SELECT id FROM runs ORDER BY started_ns DESC LIMIT 1").fetchone()[0]
        return connection.execute("SELECT COUNT(*) FROM call_records WHERE run_id = ?", (run,)).fetchone()[0]


def loop_of(root: Path):
    """The time of the calls alone, for a fixture that writes it (rich-scaled), else None."""
    file = root / "loop_seconds.txt"
    if not file.exists():
        return None
    value = float(file.read_text())
    file.unlink()
    return value


def measure(name, fixture, rounds, checkouts, work):
    root, plain, traced = fixture
    times = {"plain": []} | {label: [] for label in checkouts}
    loops = {label: [] for label in ["plain", *checkouts]}
    calls = {}
    for _ in range(rounds):
        overhead.clean(root)
        times["plain"].append(overhead.timed(plain, root, overhead.environment(overhead.REPO, root)))
        loops["plain"].append(loop_of(root))
        for label, checkout in checkouts.items():
            env = overhead.environment(Path(checkout), root)
            overhead.clean(root)
            overhead.timed(traced, root, env)  # the previous run
            loop_of(root)
            times[label].append(overhead.timed(traced, root, env))
            loops[label].append(loop_of(root))
            calls[label] = calls_of(root)
    # What the comparison costs on the recording thread, timed from inside (one more run each).
    inside = {}
    for label, checkout in checkouts.items():
        env = overhead.environment(Path(checkout), root)
        overhead.clean(root)
        overhead.timed(traced, root, env)
        env["PYTHONPATH"] += os.pathsep + str(Path(__file__).resolve().parent / "lookup_probe")
        env["LDR_LOOKUP_OUT"] = str(root / "lookup.json")
        for old in glob.glob(str(root / "lookup.json.*")):
            os.unlink(old)
        overhead.timed(traced, root, env)
        found = [json.load(open(path)) for path in glob.glob(str(root / "lookup.json.*"))]
        inside[label] = {
            "load_s": round(sum(item["load_s"] for item in found), 3),
            "compare_s": round(sum(item["compare_s"] for item in found), 3),
            "compare_calls": sum(item["compare_calls"] for item in found),
        }
        for item in glob.glob(str(root / "lookup.json.*")):
            os.unlink(item)
    if all(value is not None for series in loops.values() for value in series):
        loop_row = {label: round(statistics.median(series), 4) for label, series in loops.items()}
    else:
        loop_row = None
    row = {"fixture": name, "calls": calls, "inside_s": inside, "loop_median_s": loop_row, "median_s": {k: round(statistics.median(v), 3) for k, v in times.items()},
           "all_s": {k: [round(t, 3) for t in v] for k, v in times.items()}}
    labels = list(checkouts)
    reference = labels[0]
    plain_median = statistics.median(times["plain"])
    row["default_over_plain"] = {label: round(statistics.median(times[label]) / plain_median, 2) for label in labels}
    row["against_" + reference] = {
        label: {
            "added_s": round(statistics.median(times[label]) - statistics.median(times[reference]), 3),
            "us_per_call": round((statistics.median(times[label]) - statistics.median(times[reference])) / calls[label] * 1e6, 1),
            "percent_of_time_ldr_adds": round(
                100 * (statistics.median(times[label]) - statistics.median(times[reference]))
                / (statistics.median(times[reference]) - plain_median), 1
            ),
        }
        for label in labels[1:]
    }
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", action="append", required=True, metavar="LABEL=PATH")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--work", default=str(Path(overhead.tempfile.gettempdir()) / "ldr-overhead"))
    parser.add_argument("--only", nargs="+", choices=overhead.NAMES, default=["rich-scaled", "orders", "toolz"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    work = Path(args.work)
    results = []
    for name in args.only:
        checkouts = dict(item.split("=", 1) for item in args.checkout)
        results.append(measure(name, overhead.build(work, name), args.rounds, checkouts, work))
        print(json.dumps(results[-1]) if args.json else results[-1], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
