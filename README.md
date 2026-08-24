# LDR

[![Core tests](https://github.com/aymen-cherife/LDR/actions/workflows/ci.yml/badge.svg)](https://github.com/aymen-cherife/LDR/actions/workflows/ci.yml)

**Runtime evidence for Python coding agents.**

LDR records function-boundary execution without changing the target source, then produces a bounded failure trace, a focused local-state history, or a behavioral diff between runs. It is built on Python's `sys.monitoring` API.

Instead of asking an agent to guess where execution diverged, LDR gives it structured evidence: calls, returns, exceptions, failure paths, source locations, and ranked changes.

## Install

LDR requires Python 3.12+.

```bash
pip install -e .
```

## Agent workflow

Start with one failing test:

```bash
ldr pytest tests/test_orders.py::test_refund --only src tests
```

Pytest collection is excluded from the trace, so imports do not consume the runtime budget. The result includes stable frame IDs. Select a suspicious invocation and inspect only its local-state transitions:

```bash
ldr inspect tests/test_orders.py::test_refund \
  --frame 'src/orders.py:42:calculate_refund#1' \
  --only src tests
```

LDR preserves the test runner's exit status and writes:

- `.ldr/last.json` — structured output for agents and tooling
- `.ldr/last.txt` — a compact human-readable trace

For a stateful function called many times, use the function's
`inspect_selector` from `.ldr/last.json` to inspect every invocation in one
rerun:

```bash
ldr inspect tests/test_orders.py::test_refund \
  --frame 'src/orders.py:42:calculate_refund#*' \
  --only src tests
```

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
--max-calls-per-function 8  Sample repetitive hot calls after the first eight
--max-lines 200             Bound the prose output
--frame FRAME_ID            Inspect one invocation or a function selector ending #*
--max-state-steps 200       Bound focused local-state history
--learn-volatility          Mask fields that vary across repeated runs
--fail-on-change            Exit with code 3 when behavior changes
--golden                    Replace the current snapshot
```

Values and output are bounded, common credential fields are redacted, and result files are written atomically with owner-only permissions.

## Evidence and integrations

- [Agent skill](agents/ldr-debug-runtime/SKILL.md)
- [Paired evaluation harness](evaluation/README.md)
- [Latest SWE-bench-Live smoke test](evaluation/results/hypothesis-4532.md)
- [Hard random benchmark sample](evaluation/results/hard-random-sample-20260824.md)
- [Competitive analysis](docs/competitive-analysis.md)

LDR 0.5 is still a prototype. The current goal is to measure whether structured runtime evidence helps coding agents resolve real failures with fewer investigation turns—not to maximize debugger features.
