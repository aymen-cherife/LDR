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
import marshal
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


def load_source(ldr_dir: str, prefix: str | None, address: str) -> dict | None:
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
            root = (
                connection.execute(
                    "SELECT error FROM call_records WHERE run_id = ? AND frame_id = ?", (run_id, prefix)
                ).fetchone()
                if prefix is not None else (status.get("exit_code") or None,)  # a script: how it ended
            )
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
    """The command line of a rerun: the nodes in order, the original options, the seed.

    A script (no nodes) is rerun whole, with its own arguments.
    """
    if not nodes:
        return [invocation["runner"], invocation["target"], *invocation.get("arguments", [])]
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


def _window(connection, run_id: str) -> tuple[float, float]:
    """A run's own start-to-end time as epoch seconds."""
    started_ns, status = connection.execute("SELECT started_ns, status FROM runs WHERE id = ?", (run_id,)).fetchone()
    timing = (json.loads(status or "{}").get("timing") or {})
    seconds = timing.get("total_seconds") or timing.get("execution_seconds") or 0
    return started_ns / 1e9 - 1, started_ns / 1e9 + seconds + 1


_TIME_TYPES = ("datetime.datetime(", "datetime.date(", "datetime.time(", "time.struct_time(")


def is_time_like(preview: str, windows: list[tuple[float, float]]) -> bool:
    """A datetime, date, time or struct_time, or a number inside the run's own epoch window.

    The number is tried as seconds, milliseconds, microseconds and nanoseconds.
    """
    text = preview.strip()
    if text.startswith(_TIME_TYPES):
        return True
    try:
        number = float(text)
    except ValueError:
        return False
    return any(
        start * scale <= number <= end * scale
        for start, end in windows for scale in (1, 1e3, 1e6, 1e9)
    )


def time_like_differences(ldr_dir: str, source_run: str, rerun_run: str, prefix: str | None) -> int | None:
    """How many values differ between the two runs if every one of them is time-like, else None."""
    with _connect(ldr_dir) as connection:
        def parts(run_id):
            query = "SELECT frame_id, parts FROM call_records WHERE run_id = ?"
            arguments = [run_id]
            if prefix:
                query += " AND (frame_id = ? OR substr(frame_id, 1, ?) = ?)"
                arguments += [prefix, len(prefix) + 1, prefix + "/"]
            return {address: marshal.loads(blob) for address, blob in connection.execute(query, arguments)}

        before, after = parts(source_run), parts(rerun_run)
        if before.keys() != after.keys():
            return None  # a call that is new or missing is not a time-like difference
        differing = []
        for address, left in before.items():
            old = {(role, name): token for role, name, token in left}
            new = {(role, name): token for role, name, token in after[address]}
            differing += [(address, key) for key in old.keys() | new.keys() if old.get(key) != new.get(key)]
        if not differing:
            return None
        windows = [_window(connection, source_run), _window(connection, rerun_run)]
        details = {}
        for address in {address for address, _ in differing}:
            row = connection.execute(
                "SELECT payload FROM stream_calls WHERE run_id = ? AND frame_id = ?", (rerun_run, address)
            ).fetchone()
            details[address] = json.loads(row[0]) if row else {}
    for address, (role, name) in differing:
        detail = details.get(address, {})
        if role == "in":
            preview = (detail.get("input_preview") or {}).get(name)
        elif role == "ret":
            preview = detail.get("output_preview")
        else:
            return None  # a value changed during the call: its preview is not kept
        if preview is None or not is_time_like(str(preview), windows):
            return None
    return len(differing)


def run_detail(
    address: str,
    ldr_dir: str,
    launch: Callable[[list[str], str | None], int],
    current_environment: dict | None = None,
) -> int:
    """Rerun for detail. ``launch(argv, hash_seed)`` runs one LDR child and returns its status."""
    found = test_of(address)
    is_script = found is None
    prefix, node = found if found else (None, None)
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
    runner = source["invocation"].get("runner")
    if is_script and runner == "pytest":
        print(f"{address} is not a call inside a test of the recorded pytest run.")
        return 2
    if not is_script and runner != "pytest":
        print(f"{address} names a pytest test, but the recorded run was not a pytest run.")
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
    states: dict[str, str] = {}
    if is_script:
        # The whole script is the unit; the order rules are for pytest only.
        attempts = [([], "the whole script")]
    else:
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
            "random_start": source["rng"].get("random-at-start") if is_script else None,
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
                f"RERUN VERIFIED ({how}): {summary}. Every call of {node or 'the script'} kept its detail; "
                f"read it with: ldr show {address}"
            )
            return 0
        if source["test_failed"] and code == 0:
            print(
                f"failed, then passed on rerun: flaky ({node or 'the script'} failed in run {source['run_id']} "
                "and passed when rerun). No detail is shown: the rerun is not the original execution."
            )
            return 5
    advice = (
        f"Record this test with full detail from the start: ldr pytest {node} --full={node}"
        if node else f"Record the script with full detail from the start: ldr run {source['invocation']['target']} --full"
    )
    time_like = None
    if last_status.get("run_id"):
        try:
            time_like = time_like_differences(ldr_dir, source["run_id"], last_status["run_id"], prefix)
        except (sqlite3.Error, ValueError, KeyError):
            time_like = None
    if time_like:
        print(
            f"could not reproduce: the {time_like} value(s) that differ are time-like (a datetime, date, "
            f"time, or a number inside the run's own time window), so the code likely depends on the clock "
            f"({summary}). No detail is shown from a rerun that did not match run {source['run_id']}. {advice}"
        )
        return 5
    print(
        f"could not reproduce: likely order / time / network dependence ({summary}). "
        f"No detail is shown from a rerun that did not match run {source['run_id']}. {advice}"
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
