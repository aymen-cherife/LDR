"""Run one isolated baseline or LDR-assisted agent evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from ldr import processes  # noqa: E402


HERE = Path(__file__).resolve().parent


def _usage(value: Any) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower()
            if normalized in {"input_tokens", "inputtokens", "prompt_tokens"} and isinstance(item, int):
                input_tokens += item
            elif normalized in {"output_tokens", "outputtokens", "completion_tokens"} and isinstance(item, int):
                output_tokens += item
            else:
                left, right = _usage(item)
                input_tokens += left
                output_tokens += right
    elif isinstance(value, list):
        for item in value:
            left, right = _usage(item)
            input_tokens += left
            output_tokens += right
    return input_tokens, output_tokens


def _parse_usage(output: str) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        left, right = _usage(value)
        input_tokens += left
        output_tokens += right
    return input_tokens, output_tokens


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-json", required=True, type=Path)
    parser.add_argument("--condition", required=True, choices=("baseline", "ldr"))
    parser.add_argument("--command", required=True, help="headless agent command; prompt is sent on stdin")
    parser.add_argument("--output-dir", type=Path, default=HERE / "runs")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    task = json.loads(args.task_json.read_text())
    condition_text = (HERE / "prompts" / f"{args.condition}.md").read_text().strip()
    problem = task.get("problem_statement") or task.get("title") or task["instance_id"]
    prompt = f"{problem.strip()}\n\nEvaluation instructions:\n{condition_text}\n"

    before = processes.run(
        ["git", "status", "--porcelain"], cwd=workspace, text=True, capture_output=True, check=True
    ).stdout
    if before.strip():
        parser.error("workspace must be a clean, isolated checkout")

    run_id = f"{task['instance_id']}--{args.condition}--{int(time.time())}"
    run_dir = args.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    timed_out = False
    try:
        result = processes.run(
            shlex.split(args.command),
            cwd=workspace,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=args.timeout,
            env=dict(os.environ, LDR_EVAL_CONDITION=args.condition),
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    duration = time.monotonic() - started

    diff = processes.run(
        ["git", "diff", "--binary"], cwd=workspace, text=True, capture_output=True, check=True
    ).stdout
    status = processes.run(
        ["git", "status", "--porcelain"], cwd=workspace, text=True, capture_output=True, check=True
    ).stdout
    input_tokens, output_tokens = _parse_usage(stdout)
    (run_dir / "stdout.log").write_text(stdout)
    (run_dir / "stderr.log").write_text(stderr)
    (run_dir / "patch.diff").write_text(diff)
    summary = {
        "run_id": run_id,
        "instance_id": task["instance_id"],
        "condition": args.condition,
        "command": args.command,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 2),
        "input_tokens_observed": input_tokens,
        "output_tokens_observed": output_tokens,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "changed_files": [line[3:] for line in status.splitlines() if len(line) >= 4],
        "patch_bytes": len(diff.encode()),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
