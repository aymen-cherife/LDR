"""Fetch one public SWE-bench-Live row and prepare a clean checkout."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


DATASET = "SWE-bench-Live/SWE-bench-Live"
ROWS_API = "https://datasets-server.huggingface.co/rows"


def fetch_instance(instance_id: str, split: str = "full") -> dict:
    for offset in range(0, 10_000, 100):
        query = urllib.parse.urlencode(
            {
                "dataset": DATASET,
                "config": "default",
                "split": split,
                "offset": offset,
                "length": 100,
            }
        )
        with urllib.request.urlopen(f"{ROWS_API}?{query}", timeout=60) as response:
            payload = json.load(response)
        for item in payload.get("rows", []):
            row = item.get("row", {})
            if row.get("instance_id") == instance_id:
                return row
        if len(payload.get("rows", [])) < 100:
            break
    raise LookupError(f"instance not found in {DATASET}/{split}: {instance_id}")


def run(command: list[str], cwd: Path | None = None, stdin: str | None = None) -> None:
    subprocess.run(command, cwd=cwd, input=stdin, text=True, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("instance_id")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-json", required=True, type=Path)
    parser.add_argument("--split", default="full")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if workspace.exists():
        parser.error(f"refusing to overwrite existing path: {workspace}")

    row = fetch_instance(args.instance_id, args.split)
    run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            f"https://github.com/{row['repo']}.git",
            str(workspace),
        ]
    )
    run(["git", "checkout", "--detach", row["base_commit"]], cwd=workspace)
    test_patch = row.get("test_patch", "")
    if test_patch:
        run(["git", "apply", "--whitespace=nowarn", "-"], cwd=workspace, stdin=test_patch)
        run(["git", "config", "user.name", "LDR Evaluation"], cwd=workspace)
        run(["git", "config", "user.email", "ldr-eval@example.invalid"], cwd=workspace)
        run(["git", "add", "-A"], cwd=workspace)
        run(["git", "commit", "-m", "Apply benchmark test patch"], cwd=workspace)

    args.task_json.parent.mkdir(parents=True, exist_ok=True)
    args.task_json.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    print(f"Prepared {args.instance_id} at {workspace}")
    print(f"Task metadata: {args.task_json.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
