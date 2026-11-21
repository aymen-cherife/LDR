"""Default-mode overhead on five fixtures: plain run against `ldr`, median of N rounds.

    python evaluation/overhead.py [--rounds 3] [--work DIR] [--only NAME ...]
                                  [--ldr CHECKOUT] [--noise] [--json]

Fixtures (every one is downloaded into the work directory and pinned; none is copied
into this repository):

    attrs    attrs 26.1.0 (MIT), a subset of its own tests     small-call library
    flask    Flask 3.1.3 (BSD-3), its own tests                framework
    rich     rich 15.0.0 (MIT, GitHub tag; the sdist has no tests), 9 test files   object-heavy
    toolz    toolz 1.0.0 (BSD-3), its own tests                extreme: many tiny calls
    orders   tests/fixtures/order_workflow with 20,000 orders  extreme: worst case

The libraries' dependencies go into a virtual environment in the work directory from
evaluation/bench-requirements.txt. The library under test is not installed there; its
source runs from the unpacked archive, so it is project code for LDR.

Every run is a fresh process. Plain and traced rounds are interleaved, and the .ldr
directory is removed before each traced run. Wall time includes interpreter start-up.

--noise runs the same code twice and reports how many stored tables or answers differ
(see dump_run.py); it must be 0. --ldr measures another checkout of LDR (for before
and after comparisons).
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
ORDERS = 20_000
RICH_TAG = "https://github.com/Textualize/rich/archive/refs/tags/v15.0.0.tar.gz"
NAMES = ["attrs", "flask", "rich", "toolz", "orders"]

ATTRS_TESTS = [
    "test_validators.py", "test_converters.py", "test_dunders.py", "test_next_gen.py",
    "test_filters.py", "test_hooks.py", "test_init_subclass.py", "test_setattr.py",
    "test_slots.py", "test_annotations.py",
]
FLASK_SKIP = [
    "tests/test_cli.py::test_get_version", "tests/test_reqctx.py::test_bad_environ_raises_bad_request",
]
# The whole rich suite is not usable here: LDR had recorded 409,000 calls after 30 minutes
# (plain: 13 s), because rich calls pass large Console and theme objects around and nothing
# caps the fingerprint walk. This subset keeps the cheap files, the pathological test_table
# and test_columns, and object-heavy test_console and test_pretty.
RICH_TESTS = [
    "test_text", "test_segment", "test_style", "test_markup", "test_table", "test_tree",
    "test_columns", "test_console", "test_pretty",
]


def environment(ldr: Path, root: Path | None = None) -> dict[str, str]:
    path = [str(ldr)]
    if root is not None and (root / "src").is_dir():
        path.append(str(root / "src"))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(path))
    env.pop("VIRTUAL_ENV", None)
    # The order fixture reads these; the key is a fake credential that LDR must redact.
    env.update(EXPORT_API_KEY="fixture-export-key-596841", EXPORT_RATE="100")
    return env


def bench_python(work: Path) -> str:
    venv = work / "benchenv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "-r", str(REPO / "evaluation" / "bench-requirements.txt")],
            check=True,
        )
    return str(python)


def unpack(work: Path, python: str, name: str, version: str, github: str | None = None) -> Path:
    root = work / "src" / f"{name.lower()}-{version}"
    if root.exists():
        return root
    download = work / "dl"
    download.mkdir(parents=True, exist_ok=True)
    if github:
        archive = download / f"{name.lower()}-v{version}.tar.gz"
        subprocess.run(["curl", "-sL", "-o", str(archive), github], check=True)
    else:
        subprocess.run(
            [python, "-m", "pip", "download", f"{name}=={version}", "--no-binary", ":all:",
             "--no-deps", "-q", "-d", str(download)],
            check=True,
        )
        archive = next(download.glob(f"{name.lower()}-{version}.tar.gz"))
    with tarfile.open(archive) as source:
        source.extractall(work / "src", filter="data")
    return root


def pytest_fixture(work, name, version, paths, passthrough, github=None, package=None):
    python = bench_python(work)
    root = unpack(work, python, package or name, version, github)
    plain = [python, "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider", *passthrough]
    traced = [python, "-m", "ldr", "pytest", *paths, "--", "-p", "no:cacheprovider", *passthrough]
    return root, plain, traced


def build(work: Path, name: str):
    if name == "attrs":
        paths = [f"tests/{test}" for test in ATTRS_TESTS]
        return pytest_fixture(work, "attrs", "26.1.0", paths, ["--hypothesis-seed=0"])
    if name == "flask":
        skip = [arg for item in FLASK_SKIP for arg in ("--deselect", item)]
        return pytest_fixture(work, "Flask", "3.1.3", ["tests"], skip)
    if name == "rich":
        paths = [f"tests/{test}.py" for test in RICH_TESTS]
        return pytest_fixture(work, "rich", "15.0.0", paths, [], github=RICH_TAG)
    if name == "toolz":
        return pytest_fixture(work, "toolz", "1.0.0", ["toolz/tests"], [])
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


def timed(command: list[str], cwd: Path, env: dict[str, str]) -> float:
    started = time.perf_counter()
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed in {cwd}:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
    return elapsed


def clean(root: Path) -> None:
    for leftover in (".ldr", ".hypothesis"):
        shutil.rmtree(root / leftover, ignore_errors=True)


def measure(name, fixture, rounds, env):
    root, plain, traced = fixture
    times = {"plain": [], "ldr": []}
    for _ in range(rounds):
        clean(root)
        times["plain"].append(timed(plain, root, env))
        clean(root)
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


def noise(name, fixture, env):
    """Run the same code twice under LDR and count the stored tables or answers that differ."""
    root, _, traced = fixture
    dumps = []
    for _ in range(2):
        clean(root)
        timed(traced, root, env)
        result = subprocess.run(
            [sys.executable, str(REPO / "evaluation" / "dump_run.py"), str(root)],
            capture_output=True, text=True, check=True,
        )
        dumps.append(json.loads(result.stdout))
    different = [key for key in sorted(set(dumps[0]) | set(dumps[1])) if dumps[0].get(key) != dumps[1].get(key)]
    return {"fixture": name, "differences": len(different), "which": different}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
    parser.add_argument("--only", nargs="+", choices=NAMES)
    parser.add_argument("--ldr", default=str(REPO), help="checkout of LDR to measure")
    parser.add_argument("--noise", action="store_true", help="A/A check instead of timing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    work = Path(args.work)
    results = []
    for name in args.only or NAMES:
        fixture = build(work, name)
        env = environment(Path(args.ldr), fixture[0])
        if args.noise:
            results.append(noise(name, fixture, env))
        else:
            results.append(measure(name, fixture, args.rounds, env))
        item = results[-1]
        if not args.json:
            if args.noise:
                print(f"{item['fixture']:8s} A/A differences: {item['differences']} {item['which']}", flush=True)
            else:
                print(
                    f"{item['fixture']:8s} plain {item['plain_median_s']:6.2f}s  "
                    f"ldr {item['ldr_median_s']:6.2f}s  ratio {item['ratio']:5.2f}x",
                    flush=True,
                )
    if args.json:
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
