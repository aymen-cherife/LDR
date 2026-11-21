"""Phase 2 rules 9 to 11: get the detail that was not kept by rerunning one test.

``ldr run --detail <address>`` finds the test that contains the address in the latest
recorded run, and reruns only that test with what the run recorded: the same hash seed,
the same state of ``random`` and numpy, and (when pytest-randomly was active) the same
seed. Every call of the rerun is compared with the original run's, and the rerun is
believed only when they all match. If they do not, it replays the tests that ran before
this one, in their original order, and checks again. If that fails too, it says it could
not reproduce, and it never shows detail from a rerun that did not match.

This module never executes the target itself; the rerun is a normal LDR run in a child
process (rule: only ``run`` executes code).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from . import environment as run_environment
from . import reproduce

RERUN_ENV = "LDR_RERUN"
PLAN_FILE = "rerun-plan.json"
ENVIRONMENT_DIFFERS = 4
BLOCKING = ("python", "platform", "package", "value")  # kinds of environment change

_TEST_KEY = re.compile(r"(?:[^/:]+/)*[^/]*?::[^/]*")


def test_of(address: str) -> tuple[str, str] | None:
    """(the test's address prefix, its node id) for an address inside a pytest test."""
    match = _TEST_KEY.match(address)
    if not match or "::" not in match.group(0):
        return None
    prefix = match.group(0)
    path, _, rest = prefix.partition("::")
    return prefix, f"{path}::{unquote(rest)}"


def _connect(ldr_dir: str):
    path = Path(ldr_dir, "traces.sqlite3").resolve().as_uri()
    return closing(sqlite3.connect(path + "?mode=ro", uri=True))


def load_source(ldr_dir: str, prefix: str, address: str) -> dict | None:
    """The latest recorded run that is not itself a rerun, with what a rerun needs from it."""
    with _connect(ldr_dir) as connection:
        for run_id, invocation, status in connection.execute(
            "SELECT id, invocation, status FROM runs WHERE status IS NOT NULL "
            "ORDER BY started_ns DESC, id DESC"
        ):
            status = json.loads(status)
            if "rerun" in status or "reproducibility" not in status:
                continue
            tests = connection.execute(
                "SELECT worker, position, node_id, random_state FROM run_tests "
                "WHERE run_id = ? ORDER BY worker, position", (run_id,),
            ).fetchall()
            rng = dict(connection.execute("SELECT name, payload FROM run_rng WHERE run_id = ?", (run_id,)))
            row = connection.execute(
                "SELECT payload FROM run_environment WHERE run_id = ?", (run_id,)
            ).fetchone()
            known = connection.execute(
                "SELECT error FROM call_records WHERE run_id = ? AND frame_id = ?", (run_id, address)
            ).fetchone()
            kept = connection.execute(
                "SELECT 1 FROM stream_calls WHERE run_id = ? AND frame_id = ?", (run_id, address)
            ).fetchone()
            root = connection.execute(
                "SELECT error FROM call_records WHERE run_id = ? AND frame_id = ?", (run_id, prefix)
            ).fetchone()
            return {
                "run_id": run_id,
                "invocation": json.loads(invocation),
                "status": status,
                "tests": tests,
                "rng": rng,
                "environment": json.loads(row[0]) if row else None,
                "address_known": known is not None,
                "detail_kept": kept is not None,
                "test_failed": bool(root and root[0]),
            }
    return None


def rerun_arguments(invocation: dict, nodes: list[str], seed) -> list[str]:
    """The command line of a rerun: the nodes in order, the original options, the seed."""
    options = []
    skip = False
    for argument in invocation.get("arguments", []):
        if skip:
            skip = False
        elif argument in ("-n", "--numprocesses", "--dist"):
            skip = True  # a rerun is serial
        elif argument.startswith(("-n", "--numprocesses=", "--dist=")) and not argument.startswith("--no"):
            continue
        else:
            options.append(argument)
    return ["pytest", *nodes, "--", *options, *reproduce.randomly_arguments(seed)]


def verified(status: dict) -> tuple[bool, str]:
    """Whether the rerun's calls all matched the original's, and a short account of it."""
    reference = status.get("reference")
    if not reference or reference.get("error"):
        return False, "the original run's calls could not be read for comparison"
    counts = {key: reference.get(key) for key in ("compared", "same", "different", "new", "missing")}
    summary = (
        f"{counts['same']} of {counts['compared']} call(s) matched, {counts['different']} differ, "
        f"{counts['new']} new, {counts['missing']} missing"
    )
    ok = bool(counts["compared"]) and not (counts["different"] or counts["new"] or counts["missing"])
    return ok, summary


def run_detail(
    address: str,
    ldr_dir: str,
    launch: Callable[[list[str], str | None], int],
    current_environment: dict | None = None,
) -> int:
    """Rerun for detail. ``launch(argv, hash_seed)`` runs one LDR child and returns its status."""
    found = test_of(address)
    if found is None:
        print(
            f"run --detail needs the address of a call inside a pytest test (got {address!r}); "
            "a script has no test to rerun alone.",
        )
        return 2
    prefix, node = found
    if not os.path.exists(os.path.join(ldr_dir, "traces.sqlite3")):
        print("There is no recorded run to rerun. Record one first.")
        return 2
    source = load_source(ldr_dir, prefix, address)
    if source is None:
        print("There is no recorded run that can be rerun (none was recorded by this version).")
        return 2
    if not source["address_known"] and address != prefix:
        print(f"{address} is not a call in run {source['run_id']}.")
        return 2
    if source["detail_kept"]:
        print(f"Detail for {address} is already stored in run {source['run_id']}: ldr show {address}")
        return 0
    if source["invocation"].get("runner") != "pytest":
        print("run --detail reruns a pytest test; the recorded run was not a pytest run.")
        return 2
    if any("<secret" in str(argument) for argument in source["invocation"].get("arguments", [])):
        print(
            "The recorded run's arguments contained credentials that LDR redacted, so it cannot "
            "rerun them."
        )
        return 2
    if current_environment is not None and source["environment"] is not None:
        differences = [
            change for change in run_environment.compare(source["environment"], current_environment)
            if change["kind"] in BLOCKING
        ]
        if differences:
            listed = "; ".join(run_environment.describe_change(change) for change in differences[:5])
            more = f" (+{len(differences) - 5} more)" if len(differences) > 5 else ""
            print(f"could not reproduce: the environment differs from run {source['run_id']}: {listed}{more}")
            return 4

    status = source["status"]
    hash_seed = (status.get("reproducibility", {}).get("hash_seed") or {}).get("value")
    if hash_seed is None:
        print(
            f"Note: run {source['run_id']} did not record a hash seed, so set iteration order "
            "is not reproduced."
        )
    randomly = None
    if (status.get("reproducibility", {}).get("random") or {}).get("owner") == "pytest-randomly":
        randomly = source["rng"].get("pytest-randomly")
    tests = source["tests"]
    mine = next((row for row in tests if row[2] == node), None)
    if mine is None:
        print(f"Run {source['run_id']} did not record that {node} started, so its order is unknown.")
        return 2
    worker, position = mine[0], mine[1]
    before = [row[2] for row in tests if row[0] == worker and row[1] < position]
    states = {row[2]: row[3] for row in tests if row[3] is not None}
    attempts = [([node], "alone")]
    if before:
        attempts.append(([*before, node], f"after the {len(before)} test(s) that ran before it"))

    last_status: dict = {}
    summary = ""
    for number, (nodes, how) in enumerate(attempts, 1):
        plan = {
            "source_run": source["run_id"],
            "prefix": prefix,
            "node": node,
            "attempt": number,
            "random": {name: states[name] for name in nodes if name in states},
            "numpy": source["rng"].get("numpy-at-import"),
        }
        plan_path = os.path.join(ldr_dir, PLAN_FILE)
        with open(plan_path, "w", encoding="utf-8") as handle:
            json.dump(plan, handle)
        os.environ[RERUN_ENV] = plan_path
        try:
            code = launch(rerun_arguments(source["invocation"], nodes, randomly), hash_seed)
        finally:
            os.environ.pop(RERUN_ENV, None)
            try:
                os.remove(plan_path)
            except OSError:
                pass
        if code == ENVIRONMENT_DIFFERS:
            return code  # the child said so before it ran anything
        last_status = _latest_status(ldr_dir)
        ok, summary = verified(last_status)
        _mark(ldr_dir, last_status.get("run_id"), ok)
        if ok:
            print(
                f"RERUN VERIFIED ({how}): {summary}. Every call of {node} kept its detail; "
                f"read it with: ldr show {address}"
            )
            return 0
        if source["test_failed"] and code == 0:
            print(
                f"failed, then passed on rerun: flaky ({node} failed in run {source['run_id']} and "
                "passed when rerun). No detail is shown: the rerun is not the original execution."
            )
            return 5
    print(
        f"could not reproduce: likely order / time / network dependence ({summary}). "
        f"No detail is shown from a rerun that did not match run {source['run_id']}. "
        f"Record this test with full detail from the start: ldr pytest {node} --full={node}"
    )
    return 5


def _mark(ldr_dir: str, run_id: str | None, matched: bool) -> None:
    """Record whether a rerun matched, so that `show` never presents one that did not."""
    if not run_id:
        return
    try:
        with closing(sqlite3.connect(os.path.join(ldr_dir, "traces.sqlite3"))) as connection, connection:
            connection.execute(
                "UPDATE runs SET status = json_set(status, '$.rerun.verified', json(?)) WHERE id = ?",
                ("true" if matched else "false", run_id),
            )
    except sqlite3.Error:
        pass


def _latest_status(ldr_dir: str) -> dict:
    try:
        with open(os.path.join(ldr_dir, "rerun.json"), encoding="utf-8") as handle:
            return json.load(handle).get("status", {})
    except (OSError, ValueError):
        return {}


def load_plan(environ) -> dict | None:
    """Child side: the plan a rerun was started with. The variable is removed for the target."""
    path = environ.pop(RERUN_ENV, None)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
