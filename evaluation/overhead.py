"""Default-mode overhead on two fixtures: plain run against `ldr`, median of N rounds.

    python evaluation/overhead.py [--rounds 3] [--work DIR] [--only worst|typical]

Worst case: the order-export fixture with 20,000 orders (tests/fixtures/order_workflow),
about 20,000 tiny calls. Typical: the test suite of toolz 1.0.0 (BSD), a small pure
Python project. Its source distribution is downloaded once into the work directory
with pip; it is not copied into this repository.

Every run is a fresh process. Plain and traced rounds are interleaved, and the
.ldr directory is removed before each traced run. Wall time includes interpreter
start-up on both sides.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLZ = "toolz==1.0.0"
ORDERS = 20_000


def environment() -> dict[str, str]:
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env.pop("VIRTUAL_ENV", None)
    # The order fixture reads these; the key is a fake credential that LDR must redact.
    env.update(EXPORT_API_KEY="fixture-export-key-596841", EXPORT_RATE="100")
    return env


def worst_case(work: Path) -> tuple[Path, list[str], list[str]]:
    root = work / "orders"
    if not (root / "orders.csv").exists():
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / "tests" / "fixtures" / "order_workflow" / "app.py", root / "app.py")
        rng = random.Random(7)
        with open(root / "orders.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["order_id", "quantity", "cents"])
            for index in range(ORDERS):
                writer.writerow([f"ORD{index:06d}", rng.randint(1, 9), rng.randint(50, 900)])
    return root, [sys.executable, "app.py"], [sys.executable, "-m", "ldr", "run", "app.py"]


def typical(work: Path) -> tuple[Path, list[str], list[str]]:
    root = work / "toolz-1.0.0"
    if not root.exists():
        download = work / "download"
        download.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "download", TOOLZ, "--no-binary", ":all:",
             "--no-deps", "-q", "-d", str(download)],
            check=True,
        )
        archive = next(download.glob("toolz-*.tar.gz"))
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
    tests = ["toolz/tests", "-q", "-p", "no:cacheprovider"]
    return root, [sys.executable, "-m", "pytest", *tests], [sys.executable, "-m", "ldr", "pytest", "toolz/tests"]


def timed(command: list[str], cwd: Path, env: dict[str, str]) -> float:
    started = time.perf_counter()
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed in {cwd}:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
    return elapsed


def measure(name, fixture, rounds, env):
    root, plain, traced = fixture
    times = {"plain": [], "ldr": []}
    for _ in range(rounds):
        times["plain"].append(timed(plain, root, env))
        shutil.rmtree(root / ".ldr", ignore_errors=True)
        times["ldr"].append(timed(traced, root, env))
    plain_median = statistics.median(times["plain"])
    traced_median = statistics.median(times["ldr"])
    return {
        "fixture": name,
        "plain_s": [round(t, 3) for t in times["plain"]],
        "ldr_s": [round(t, 3) for t in times["ldr"]],
        "plain_median_s": round(plain_median, 3),
        "ldr_median_s": round(traced_median, 3),
        "ratio": round(traced_median / plain_median, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
    parser.add_argument("--only", choices=["worst", "typical"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    work = Path(args.work)
    env = environment()
    results = []
    for name, build in (("worst", worst_case), ("typical", typical)):
        if args.only in (None, name):
            results.append(measure(name, build(work), args.rounds, env))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for item in results:
            print(
                f"{item['fixture']:8s} plain {item['plain_median_s']:6.2f}s  "
                f"ldr {item['ldr_median_s']:6.2f}s  ratio {item['ratio']:5.2f}x"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
