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
ldr pytest tests/test_orders.py::test_refund
```

Pytest collection is excluded from the trace, so imports do not consume the runtime budget. LDR detects common `src/` and flat-package layouts, ranks up to three observed frames for follow-up, and prints a short command for each. Inspect the strongest candidate with:

```bash
ldr inspect --frame-id 37
```

That command restores the test target, runner, source scope, project root, and
portable frame selector from the previous result, reruns the test, and captures
ordered local-state transitions only for that invocation.

LDR preserves the test runner's exit status and writes:

- `.ldr/last.json` — structured output for agents and tooling
- `.ldr/last.txt` — a compact human-readable trace

The suggestions are evidence-ranked inspection targets, not claimed bug
locations. Their scores use only facts from the captured execution. The JSON
contains the component signals and exact LDR execution, post-processing, and
total wall times.

For a stateful function called many times, use the function's
`inspect_selector` from `.ldr/last.json` to inspect every invocation in one
rerun. This explicit form remains available for advanced use:

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
--frame-id N                Restore and inspect a ranked frame from the latest result
--frame FRAME_ID            Inspect one invocation or a function selector ending #*
--max-state-steps 200       Bound focused local-state history
--learn-volatility          Mask fields that vary across repeated runs
--fail-on-change            Exit with code 3 when behavior changes
--golden                    Replace the current snapshot
```

Values and output are bounded and result files are written atomically.

## Secrets

LDR records the values your program computes and prints them where a model reads them. It treats all of its output as sent to a model provider, and redacts before anything is hashed, written to `.ldr/` or printed:

- values equal to (or containing) a secret-looking environment variable (`*_KEY`, `*_SECRET`, `*_TOKEN`, `PASSWORD`), labeled `<secret from env NAME>`;
- known shapes: AWS, GitHub, Stripe, Slack, OpenAI/Anthropic and Google keys, JWTs, PEM private keys, `user:password@` in URLs, secret URL query parameters, Bearer headers, labeled `<secret: github-token, 40 chars>`;
- variables, keys and attributes with compound secret names (`api_key`, `auth_token`, `password`, ...), labeled `<secret: by name, 24 chars>`. A bare `token` or `auth` is not treated as secret.

A redacted value keeps no value and no hash, so it is never compared. Each run reports how many values were redacted.

**Redaction is best effort.** It finds what it can recognize and will miss secrets with no known shape or name. Run LDR with development credentials, not production ones.

## Evidence and integrations

- [Agent skill](agents/ldr-debug-runtime/SKILL.md)
- [Paired evaluation harness](evaluation/README.md)
- [Latest SWE-bench-Live smoke test](evaluation/results/hypothesis-4532.md)
- [Haystack mutation-ranking before/after](evaluation/results/haystack-8940-mutation-ranking-052.md)
- [Hard random benchmark sample](evaluation/results/hard-random-sample-20260824.md)
- [Competitive analysis](docs/competitive-analysis.md)
- [Inspection ranking design](docs/inspection-ranking.md)

LDR 0.5.2 is still a prototype. The current goal is to measure whether structured runtime evidence helps coding agents resolve real failures with fewer investigation turns—not to maximize debugger features.
