"""Where the time of one recorded call goes, by part (inclusive times; nested parts overlap).

    python evaluation/cost_by_part.py DIR -- <ldr arguments>

Runs the real `ldr` (parent that writes, child that records) with timers installed in the
child by evaluation/cost_hook/sitecustomize.py, then prints each part's total seconds,
calls and microseconds per recorded call, next to the wall time of the whole run. The
timers cost something themselves, so use the numbers to rank the parts, not to add them.
Measurement only.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def main():
    index = sys.argv.index("--")
    directory, arguments = sys.argv[1], sys.argv[index + 1:]
    out = os.path.join(tempfile.gettempdir(), "ldr-cost.json")
    if os.path.exists(out):
        os.remove(out)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([os.path.join(HERE, "cost_hook"), REPO]), LDR_COST_OUT=out)
    started = time.perf_counter()
    subprocess.run([sys.executable, "-m", "ldr", *arguments], cwd=directory, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wall = time.perf_counter() - started
    with open(out, encoding="utf-8") as handle:
        report = json.load(handle)
    print(f"wall {wall:.2f}s for {report['recorded_calls']} recorded calls")
    for label, row in report["parts"].items():
        print(f"{label:52s} {row['seconds']:8.3f}s {row['calls']:8d} calls {row['us_per_recorded_call']:8.1f} us/call")


if __name__ == "__main__":
    main()
