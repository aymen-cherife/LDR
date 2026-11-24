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

Pytest collection is excluded from the trace, so imports do not enter the runtime trace. LDR detects common `src/` and flat-package layouts, ranks up to three observed frames for follow-up, and prints a short command for each. Inspect the strongest candidate with:

```bash
ldr inspect --frame-id 37
```

That command restores the test target, runner, source scope, project root, and
portable frame selector from the previous result, reruns the test, and captures
ordered local-state transitions only for that invocation.

LDR preserves the test runner's exit status and writes:

- `.ldr/last.json` — a short summary for agents and tooling, always under 30,000
  characters. When a part of the result is too long (many frames, findings or
  suggestions) it keeps the first entries and a `summary` block says which parts
  were cut and how many entries each had. The whole parts are in
  `.ldr/traces.sqlite3`, table `run_sections`, under the run's `status.run_id`.
- `.ldr/last.txt` — a compact human-readable trace
- `.ldr/traces.sqlite3` — recorded call history keyed by `status.run_id` in the JSON result

LDR follows pytest's configured worker count. With local `pytest-xdist` workers,
each worker records the tests it executes and saves its trace when it finishes.
The result names workers whose traces are missing.

You can tune SQLite writes in the project's `pyproject.toml`.

```toml
[tool.ldr.storage]
batch_size = 1000
busy_timeout = 30 # seconds
```

`batch_size` is the number of captured calls per transaction. `busy_timeout`
is how long a writer can wait for a lock. The database uses WAL on SQLite
releases with the WAL-reset fix and rollback journaling on earlier releases.

LDR streams completed in-scope calls into SQLite. It keeps fingerprints for
each call and stores detail according to the keep rules. An actual write
failure marks the recording incomplete and reports the committed call count,
last recorded address, available disk space, and the next command.

Set `capture_lines = true` in the same table to store an `executed_lines` list
in first-visit order for retained calls. A line appears once per call.
See the [line capture audit](evaluation/README.md#line-budget-audit) for
the workload and byte measurements.

LDR stores supported built-in scalars and containers at function entry and
exit and on return when the call keeps detail. Redacted and unsupported
values carry an explicit status. Run `ldr inspect --frame-id N` and open
`captured_values` in `.ldr/last.json` to read the values.

Use `--deep=FUNCTION` to see how locals evolve inside a function.

```sh
ldr run app.py --deep=calculate_total
ldr show
ldr show "app.py:calculate_total#1"
```

Repeat `--deep=NAME` to select more functions; a bare `--deep` means every function. The value always takes `=`, so `--deep app.py` is a target, not a function. `--full=path::test` keeps full detail for every call of one pytest test (bare `--full` means everything); `--no-prune` is the old "disable pruning of the printed report". Each run stores its mode, and comparing across modes compares fingerprints only. Use a qualified name such as
`Cart.total` or a project-relative selector such as `app.py:calculate_total`
to narrow a match. Each snapshot records locals at the next line, return,
yield, or exception unwind. Loop visits and generator continuations appear
in order. Supported values use the same typed JSON format as full value
retention. Redacted, unsupported, and partially captured values have explicit
statuses. The run and each listed frame report observed and retained counts.

A call's address is its path of `file:qualname#n` segments from the root, where
`n` counts calls of that function under the same parent, for example
`app.py:outer#2/app.py:inner#1`. Under pytest the root is the test node id.
The definition line is not part of an address, so an address survives an edit
that moves the function. Use `file:qualname#*` to select every call of a function.

`ldr show` reads SQLite without running the target. It defaults to the latest
run. Copy a frame address from its listing and use `--run-id` to pin a run,
`--offset` and `--limit` to page, and `--json` for structured output.

Use `--boundary` once per module or package to save direct calls from
project code into selected Python libraries.

```sh
ldr run app.py --boundary urllib.parse --boundary json
ldr show --boundaries
ldr show --boundaries --run-id RUN_ID --json
```

Each saved call includes the project call site, redacted bound arguments,
and the return value or exception type. A module-level site uses the file
and line as its address. Library internals and native calls such as
`sqlite3.Connection.execute` are outside this capture mode. The run reports
how many calls were observed, saved, and still unfinished.

Each run also saves its environment: the Python and platform versions, the
installed package versions, and the names of environment variables. Variable
values are never recorded. When you compare against a baseline, the output
states that the environment is the same or lists what changed, so a
dependency bump is not mistaken for a code change. An environment change
alone does not make `--fail-on-change` exit with code 3.

```sh
ldr show --environment
ldr show --environment --run-id RUN_ID --json
```

Frame inspection and the JSON frame index show elapsed, own, traced child,
and suspended wall time. Own time includes blocking or library work that
LDR did not trace. The measurements include tracing overhead. A frame still
suspended at recorder stop is marked incomplete. Timing stays outside
behaviour fingerprints.

The suggestions are evidence-ranked inspection targets, not claimed bug
locations. Their scores use only facts from the captured execution. The JSON
contains the component signals and exact LDR execution, post-processing, and
total wall times.

For a stateful function called many times, use the function's
`inspect_selector` from `.ldr/last.json` to inspect every invocation in one
rerun. This explicit form remains available for advanced use:

```bash
ldr inspect tests/test_orders.py::test_refund \
  --frame 'src/orders.py:calculate_refund#*' \
  --only src tests
```

## Compare behavior before and after a change

```bash
ldr pytest tests/test_orders.py::test_refund --only src tests

# Edit the code, then run the same command again
ldr pytest tests/test_orders.py::test_refund --only src tests
```

Every run is recorded, and each run is compared with the previous run of the same target. The output names the functions whose calls changed. A pytest run is compared with the latest earlier pytest run that shares test node ids; tests present in only one of the two runs are listed as not compared. Any other run is compared with the latest earlier run of exactly the same command (target, arguments and `--only`). When two runs of identical files differ, the difference carries the note "also varied across 2 runs with no edit". The last 2 runs of each target are kept (`keep_runs` in `[tool.ldr.storage]`).

## Useful controls

```text
--runner pytest|run|module   Select the runner explicitly
--timeout 300               Stop a hung in-process run
--max-lines 200             Bound the prose output
--frame-id N                Restore and inspect a ranked frame from the latest result
--frame FRAME_ID            Inspect one invocation or a function selector ending #*
--fail-on-change            Exit with code 3 when behavior changes
```

Printed output is compact, and result files are written atomically. On POSIX systems they are created with owner-only permissions (0600); on Windows LDR does not change file permissions.

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

LDR 0.6.0 is still a prototype. The current goal is to measure whether structured runtime evidence helps coding agents resolve real failures with fewer investigation turns—not to maximize debugger features.
