# LDR

**Runtime evidence for Python coding agents.**

LDR records function-boundary execution without changing the target source, then produces a bounded trace or a behavioral diff between runs. It is built on Python's `sys.monitoring` API.

Instead of asking an agent to guess where execution diverged, LDR gives it structured evidence: calls, returns, exceptions, failure paths, source locations, and ranked changes.

## Install

LDR requires Python 3.12+.

```bash
pip install -e .
```

## Trace a failing test

```bash
ldr pytest tests/test_orders.py::test_refund --only src tests
```

LDR preserves the test runner's exit status and writes:

- `.ldr/last.json` — structured output for agents and tooling
- `.ldr/last.txt` — a compact human-readable trace

## Compare behavior before and after a change

```bash
# Capture the current behavior
ldr verify tests/test_orders.py::test_refund --only src tests

# Edit the code, then run the same command again
ldr verify tests/test_orders.py::test_refund --only src tests
```

The second run reports what changed in the execution tree. Snapshots are scoped by runner, target, arguments, and source scope so unrelated executions are not compared accidentally.

## Useful controls

```text
--runner pytest|run|module   Select the runner explicitly
--timeout 300               Stop a hung in-process run
--max-nodes 5000            Bound the full trace
--max-lines 200             Bound the prose output
--learn-volatility          Mask fields that vary across repeated runs
--fail-on-change            Exit with code 3 when behavior changes
--golden                    Replace the current snapshot
```

Values are bounded, common credential fields are redacted, and result files are written atomically with owner-only permissions.

LDR is an early prototype. The immediate goal is to test whether structured runtime evidence helps coding agents diagnose real failures more reliably and with less wandering.
