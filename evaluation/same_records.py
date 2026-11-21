"""Run two checkouts of LDR on the same fixtures and compare what each stored and answered.

    python evaluation/same_records.py --a CHECKOUT --b CHECKOUT [--work DIR] [--only NAME ...]
                                      [--drop KEY,KEY]

Used between two speed steps: the stored records and every answer must be unchanged.
--drop names JSON keys that the step changed on purpose (for example a removed summary
field); they are removed from both sides before hashing. The hash seed is pinned so that
set ordering does not add noise; the A/A check in overhead.py measures the noise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overhead  # noqa: E402

HERE = Path(__file__).resolve().parent


def run_and_dump(name, fixture, checkout, drop):
    root, _, traced = fixture
    env = overhead.environment(Path(checkout), root)
    env["PYTHONHASHSEED"] = "0"
    overhead.clean(root)
    overhead.timed(traced, root, env)
    result = subprocess.run(
        [sys.executable, str(HERE / "dump_run.py"), str(root)],
        capture_output=True, text=True, check=True, env=dict(os.environ, DUMP_DROP=drop),
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
    parser.add_argument("--only", nargs="+", choices=overhead.NAMES)
    parser.add_argument("--drop", default="")
    args = parser.parse_args()
    work = Path(args.work)
    status = 0
    for name in args.only or overhead.NAMES:
        fixture = overhead.build(work, name)
        first = run_and_dump(name, fixture, args.a, args.drop)
        second = run_and_dump(name, fixture, args.b, args.drop)
        different = [key for key in sorted(set(first) | set(second)) if first.get(key) != second.get(key)]
        rows = {key: (first.get(key, {}).get("rows"), second.get(key, {}).get("rows")) for key in different}
        print(f"{name:8s} {'identical' if not different else 'DIFFERENT ' + str(rows)}", flush=True)
        status |= bool(different)
    return status


if __name__ == "__main__":
    sys.exit(main())
