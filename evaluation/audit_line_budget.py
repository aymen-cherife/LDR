"""Measure the serialized trace bytes needed to retain real patched lines."""

import argparse
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from ldr import processes  # noqa: E402

LDR_ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = {"PySnooper": "pysnooper", "cookiecutter": "cookiecutter", "tqdm": "tqdm"}


def git(repo, *args):
    return processes.check_output(["git", "-C", str(repo), *args])


def unpack(repo, commit, directory):
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", commit))) as archive:
        archive.extractall(directory, filter="data")


def patched_lines(repo, buggy, fixed, package):
    result = {}
    current = None
    diff = git(repo, "diff", "--no-renames", "--unified=0", buggy, fixed, "--", package)
    for line in diff.decode(errors="replace").splitlines():
        if line.startswith("--- "):
            path = line.removeprefix("--- a/")
            source = path.endswith(".py") and not path.startswith(
                ("--- ", f"{package}/tests/")
            )
            current = result.setdefault(path, set()) if source else None
        elif line.startswith("@@ ") and current is not None:
            start, length = re.match(r"@@ -(\d+)(?:,(\d+))?", line).groups()
            current.update(range(int(start), int(start) + int(length or 1)))
    return {path: lines for path, lines in result.items() if lines}


def run(cwd, arguments, env, timeout):
    return processes.run(
        [sys.executable, "-m", *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def measure(directory, gold):
    result = json.loads((directory / ".ldr/last.json").read_text(encoding="utf-8"))
    stream = result["status"]["call_stream"]
    completed = stream["started"] - stream["unfinished"]
    if stream["stored"] != completed or stream["write_failed"]:
        raise ValueError("capture incomplete at the audit ceiling")
    with closing(sqlite3.connect(directory / ".ldr/traces.sqlite3")) as connection:
        rows = list(
            connection.execute(
                "SELECT frame_id, payload FROM stream_calls WHERE run_id = ? "
                "ORDER BY json_extract(payload, '$.completion_order')",
                (result["status"]["run_id"],),
            )
        )
        (total,) = connection.execute(
            "SELECT used_bytes FROM stream_budget WHERE run_id = ?",
            (result["status"]["run_id"],),
        ).fetchone()
    used = 0
    hits = {}
    for frame, raw in rows:
        used += len(frame.encode("utf-8")) + len(raw)
        path = frame.rsplit(":", 2)[0]
        for line in gold.get(path, set()).intersection(
            json.loads(raw)["executed_lines"]
        ):
            hits.setdefault((path, line), used)
    if used != total or len(rows) != stream["stored"]:
        raise ValueError("stream rows and byte accounting disagree")
    return {
        "calls": len(rows),
        "unfinished_calls": stream["unfinished"],
        "trace_bytes": used,
        "first_hit_bytes": min(hits.values(), default=None),
        "last_hit_bytes": max(hits.values(), default=None),
        "patched_lines_executed": len(hits),
        "patched_lines": sum(map(len, gold.values())),
    }


def audit_case(root, subject, case, args, env):
    repo = root / subject
    package = SUBJECTS[subject]
    versions = dict(re.findall(r'(\w+)="([^"]*)"', (case / "bug.info").read_text()))
    buggy, fixed = versions["buggy_commit_id"], versions["fixed_commit_id"]
    targets = list(
        dict.fromkeys(
            re.findall(
                r"[^\s'\";]+\.py::[^\s'\";]+", (case / "run_test.sh").read_text()
            )
        )
    )
    gold = patched_lines(repo, buggy, fixed, package)
    result = {
        "case": f"{subject}/{case.name}",
        "buggy": buggy,
        "fixed": fixed,
        "targets": targets,
    }
    if not targets or not gold:
        return result | {"outcome": "no supported target or patched old line"}
    with TemporaryDirectory(prefix="line-case-", dir=root) as scratch:
        before, after = Path(scratch) / "before", Path(scratch) / "after"
        for revision, directory in ((buggy, before), (fixed, after)):
            directory.mkdir()
            unpack(repo, revision, directory)
        test_roots = set()
        for target in targets:
            test_path = Path(target.split("::", 1)[0])
            if "tests" in test_path.parts:
                test_roots.add(
                    Path(*test_path.parts[: test_path.parts.index("tests") + 1])
                )
            else:
                test_roots.add(test_path)
        for test_path in sorted(test_roots):
            if (after / test_path).is_dir():
                shutil.copytree(
                    after / test_path, before / test_path, dirs_exist_ok=True
                )
            else:
                (before / test_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(after / test_path, before / test_path)
        with (before / "pyproject.toml").open("a", encoding="utf-8") as file:
            file.write(
                f"\n[tool.ldr.storage]\ncapture_lines = true\nmax_trace_bytes = {args.capacity}\n"
            )
        pytest_options = ["-o", "addopts="]
        fixed_run = run(
            after, ["pytest", "-q", *pytest_options, *targets], env, args.timeout
        )
        expected = run(
            before, ["pytest", "-q", *pytest_options, *targets], env, args.timeout
        )
        if (
            (expected.returncode, fixed_run.returncode) != (1, 0)
            or not re.search(r"(?m)^FAILED ", expected.stdout)
            or not re.search(r"\b[1-9]\d* passed\b", fixed_run.stdout)
        ):
            return result | {
                "outcome": "failure/pass pair did not reproduce",
                "exit_codes": [expected.returncode, fixed_run.returncode],
                "summary": (expected.stdout + expected.stderr)[-600:],
                "fixed_summary": (fixed_run.stdout + fixed_run.stderr)[-600:],
            }
        command = [
            "ldr",
            "pytest",
            targets[0],
            "--project-root",
            ".",
            "--only",
            package,
            "--",
            *pytest_options,
            *targets[1:],
        ]
        recorded = run(before, command, env, args.timeout)
        if recorded.returncode != 1 or not re.search(r"(?m)^FAILED ", recorded.stdout):
            return result | {
                "outcome": "recorded execution changed the test exit code",
                "exit_codes": [expected.returncode, recorded.returncode],
            }
        return result | {"outcome": "measured"} | measure(before, gold)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", type=Path, help="directory containing the BugsInPy and subject clones"
    )
    parser.add_argument(
        "--deps", type=Path, help="optional dependency directory added to PYTHONPATH"
    )
    parser.add_argument(
        "--capacity", type=int, required=True, help="audit ceiling in serialized bytes"
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="seconds allowed per test command"
    )
    args = parser.parse_args()
    root = args.root.resolve()
    paths = [str(LDR_ROOT)]
    if args.deps:
        paths.append(str(args.deps.resolve()))
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(paths),
        PYTHONHASHSEED="0",
        PYTHONUTF8="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
        PYTHONDONTWRITEBYTECODE="1",
    )
    rows = []
    for subject in SUBJECTS:
        cases = root / "BugsInPy/projects" / subject / "bugs"
        for case in sorted(cases.iterdir(), key=lambda path: int(path.name)):
            try:
                row = audit_case(root, subject, case, args, env)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                row = {
                    "case": f"{subject}/{case.name}",
                    "outcome": type(exc).__name__,
                    "detail": str(exc),
                }
            rows.append(row)
            print(json.dumps(row), flush=True)
    measured = [row for row in rows if row["outcome"] == "measured"]
    hits = sorted(
        row["first_hit_bytes"] for row in measured if row["first_hit_bytes"] is not None
    )
    majority = len(measured) // 2 + 1
    p90 = math.ceil(0.9 * len(measured))
    print(
        json.dumps(
            {
                "cases": len(rows),
                "measured": len(measured),
                "patch_hits": len(hits),
                "first_hit_majority_bytes": hits[majority - 1]
                if len(hits) >= majority
                else None,
                "first_hit_p90_bytes": hits[p90 - 1]
                if p90 and len(hits) >= p90
                else None,
                "ceiling_bytes": args.capacity,
            }
        )
    )


if __name__ == "__main__":
    main()
