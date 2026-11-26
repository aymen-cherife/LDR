"""Run the post-batch checks and print a summary. Full outputs stay in separate logs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from subprocess import STDOUT, CalledProcessError

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from ldr import processes

PHASES = ("wall", "python_start", "to_first_committed_batch", "program_after_first_batch")
END_PHASES = ("pruning", "return", "peak_rss_bytes", "supervisor_peak_rss_bytes")
COUNTS = re.compile(r"\b\d+ (?:passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?)\b")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def run(log, command, *, append=False):
    with log.open("ab" if append else "wb") as output:
        try:
            return processes.run(command, cwd=REPO, stdout=output, stderr=STDOUT,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                          PYTHONIOENCODING="utf-8")).returncode
        except OSError as error:
            output.write(f"{error}\n".encode("utf-8", "backslashreplace"))
            return 1


def python(*arguments):
    return [sys.executable, "-B", *map(str, arguments)]


def suite_summary(path, code):
    counts, failed = [], []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = ANSI.sub("", raw).strip()
        if " in " in line and (found := COUNTS.findall(line)):
            counts = found
        if line.startswith(("FAILED ", "ERROR ")):
            failed.append(line.split(" - ", 1)[0])
    print(f"Suite (exit {code}) | " + (", ".join(counts) or "no counts reported"))
    for test in failed:
        print(test)


def json_lines(path):
    with path.open(encoding="utf-8", errors="replace") as log:
        for number, line in enumerate(log, 1):
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield number, row


def timings_summary(path, code):
    runs = [row for _, row in json_lines(path) if "post_run_seconds" in row]
    print(f"Rich phases (exit {code})")
    if not runs:
        return
    print("| Measure | " + " | ".join(f"{r['commit'][:7]} {r['fixture']} {r['run']}" for r in runs) + " |")
    print("|---|" + "---:|" * len(runs))
    rows = [{**row, **row["post_run_seconds"]} for row in runs]
    for key in (*PHASES, *runs[0]["post_run_seconds"], *END_PHASES):
        values = [row[key] for row in rows]
        label = key
        if key.endswith("_bytes"):
            label = "Supervisor peak MiB" if key.startswith("supervisor") else "Program peak MiB"
            values = [f"{value / 1024**2:.2f}" for value in values]
        print(f"| {label} | " + " | ".join(str(value) for value in values) + " |")
    print(f"{runs[0]['platform']} · Python {runs[0]['python_version']} · {runs[0]['mode']} recording")
    for row in runs:
        print(f"{row['run']} · {row['machine']}")


def compare_summary(path, code):
    verdicts, differences = {}, []
    for number, row in json_lines(path):
        if "verdicts" in row:
            verdicts.setdefault(row["fixture"], {})[row["checkout"]] = row["verdicts"]
        elif "section" in row:
            detail = f" row {row['row']}" if "row" in row else ""
            note = " (decoded values equal)" if row.get("decoded_equal") else ""
            differences.append(f"{row['fixture']} {row['section']}{detail}{note} · {path.name}:{number}")
    result = "identical records and verdicts" if code == 0 else "differences or incomplete comparison"
    print(f"same_records (exit {code}) | {result}")
    for fixture, sides in verdicts.items():
        for side, counts in sides.items():
            print(f"{fixture} {side} · " + ", ".join(f"{name} {count}" for name, count in counts.items()))
    for difference in differences:
        print(difference)


def comparison(folder, baseline, work):
    path = folder / "same_records.log"
    checkout = folder / "baseline-checkout"
    try:
        revision = processes.check_output(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{baseline}^{{commit}}"],
            cwd=REPO, text=True, stderr=STDOUT,
        ).strip()
    except (OSError, CalledProcessError) as error:
        path.write_text(f"{error}\n{getattr(error, 'output', '')}", encoding="utf-8")
        return 1
    code = run(path, ["git", "worktree", "add", "--detach", str(checkout), revision])
    if code:
        return code
    try:
        code = run(path, python(HERE / "same_records.py", "--a", checkout, "--b", REPO,
                                "--only", "orders", "toolz", "--work", work), append=True)
    finally:
        cleaned = run(path, ["git", "worktree", "remove", str(checkout)], append=True)
    return code or cleaned


def controlled_comparison(python_reader, native_reader, work, fixture="rich"):
    """Measure both readers against copies of one recording at the same path."""
    import overhead
    import run_phases

    root, _, traced = overhead.build(work, fixture)
    overhead.clean(root)
    first_env = overhead.environment(python_reader, root)
    run_phases.one(root, traced, first_env)
    with tempfile.TemporaryDirectory(prefix="ldr-previous-") as scratch:
        previous = Path(scratch) / "previous"
        shutil.copytree(root / ".ldr", previous)
        for label, checkout in (("python", python_reader), ("native", native_reader)):
            overhead.clean(root)
            shutil.copytree(previous, root / ".ldr")
            env = overhead.environment(checkout, root)
            result = run_phases.one(root, traced, env)
            commit = processes.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
            print(json.dumps({"checkout": label, "commit": commit, "fixture": fixture,
                              "run": "controlled second", "pythonhashseed": run_phases.HASH_SEED,
                              "platform": sys.platform, "mode": "default", **result}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", default="faa55d3", nargs="?", help="commit for same_records")
    parser.add_argument("--work", default=Path(tempfile.gettempdir()) / "ldr-overhead", type=Path,
                        help="benchmark fixture cache")
    parser.add_argument("--python-reader", type=Path)
    parser.add_argument("--native-reader", type=Path)
    args = parser.parse_args()
    if bool(args.python_reader) != bool(args.native_reader):
        parser.error("--python-reader and --native-reader must be supplied together")
    commit = processes.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    folder = Path(tempfile.mkdtemp(prefix="ldr-final-check-"))
    suite, machine, phases = [folder / f"{name}.log" for name in ("suite", "machine", "rich")]
    suite_code = run(suite, python("-m", "pytest", "-q"))
    machine_code = run(machine, python("-m", "ldr", "check_machine"))
    phases_code = run(phases, python(HERE / "run_phases.py", "--checkout", f"current={REPO}",
                                     "--only", "rich", "--work", args.work.resolve()))
    compare_code = comparison(folder, args.baseline, args.work.resolve())
    controlled_code = 0
    if args.python_reader:
        try:
            controlled_comparison(args.python_reader.resolve(), args.native_reader.resolve(), args.work.resolve())
        except Exception as error:
            print(f"Controlled comparison failed: {error}", file=sys.stderr)
            controlled_code = 1

    print(f"LDR final check · {commit} · baseline {args.baseline}")
    suite_summary(suite, suite_code)
    facts = next((line for line in machine.read_text(encoding="utf-8", errors="replace").splitlines()
                  if line.startswith("machine:")), "no facts line")
    print(f"Preflight (exit {machine_code}) | {facts}")
    timings_summary(phases, phases_code)
    compare_summary(folder / "same_records.log", compare_code)
    print(f"Logs · {folder}")
    return int(any((suite_code, machine_code, phases_code, compare_code, controlled_code)))


if __name__ == "__main__":
    raise SystemExit(main())
