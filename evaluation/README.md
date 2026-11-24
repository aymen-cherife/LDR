# LDR agent evaluation

This harness compares the same coding-agent configuration with and without LDR. It measures the complete agent configuration—not the model name alone.

`tasks.json` contains eight recent SWE-bench-Live Python tasks selected for runtime-diagnostic value. Prepare two clean checkouts of the same task, including the benchmark test patch and dependencies. Install the current LDR checkout only in the assisted environment.

```bash
python evaluation/prepare_swebench_live.py HypothesisWorks__hypothesis-4532 \
  --workspace /tmp/hypothesis-ldr \
  --task-json /tmp/hypothesis-4532.json
```

The preparation command never overwrites a path. It checks out the benchmark base commit, applies and locally commits the public test patch, and leaves dependency installation to the repository or benchmark environment.

Run the same headless agent command and budget in each checkout:

```bash
python evaluation/run_agent.py \
  --workspace /tmp/task-baseline \
  --task-json /tmp/task.json \
  --condition baseline \
  --command 'claude -p --output-format stream-json'

python evaluation/run_agent.py \
  --workspace /tmp/task-ldr \
  --task-json /tmp/task.json \
  --condition ldr \
  --command 'claude -p --output-format stream-json'
```

Use clean sessions and randomize condition order. Do not share trajectories, patches, or LDR output across conditions. Run each task at least twice when cost allows.

Primary metric: gold evaluation tests resolved. Secondary metrics: wall time, observed tokens, changed files, patch size, test reruns, LDR call rate, and whether LDR identified the gold-patch function before the first application-source edit.

Internal continuation threshold: at least two additional tasks solved out of eight, or at least 25% less investigation cost without a lower solve rate. This is a development gate, not a publishable statistical claim.

## SQLite storage benchmark

Run both commands to compare one writer and four writers at the same total call count.

```bash
python evaluation/benchmark_sqlite_store.py --workers 1 --calls 100000
python evaluation/benchmark_sqlite_store.py --workers 4 --calls 25000
```

The benchmark generates call records and compares generation alone with generation plus SQLite persistence. It verifies the stored counts and an indexed lookup. Use `--batch-size`, `--distinct-values`, and `--value-bytes` to vary write frequency, reuse, and payload size. Use `--measure-memory` in a separate run to track Python allocations. The workload uses synthetic calls. End-to-end tracing overhead requires running the same real test command with tracing enabled and disabled.

On Windows with Python 3.13 and SQLite 3.50.4, these runs each wrote 100,000 calls in batches of 1,000. Disk size includes the database and its WAL and shared-memory files before closing the database.

| Writers | Values | Value table | Generation only | Generation and storage | Slowest batch | Disk |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 1 | 10,000 at 256 B | Rowid | 0.48 s | 1.51 s | 31 ms | 13.0 MB |
| 4 | 10,000 at 256 B | Rowid | 0.22 s | 1.61 s | 1.10 s | 13.9 MB |
| 4 | 100,001 at 1 KiB | Rowid | 0.40 s | 5.85 s | 2.94 s | 381 MB |
| 4 | 100,001 at 1 KiB | `WITHOUT ROWID` | 0.36 s | 79.77 s | 13.03 s | 1.72 GB |

Reproduce the last two cases with `--workers 4 --calls 25000 --distinct-values 100001 --value-bytes 1024` and select the table with `--value-layout`. The rowid table kept the indexed lookup below 1 ms in these runs. A separate memory run with large values measured about 1.27 MB of peak Python allocations per writer. SQLite's native allocations are outside that measurement.

With the same large-value workload and batches of 100, the rowid table took 8.82 s and the slowest batch took 1.34 s. These measurements capture whole transaction time, including lock waits, writes, and commits.

### End-to-end pytest recording

On Windows with Python 3.13, pytest 9.2, and xdist 3.8, I ran 24 upstream `more-itertools` tests from commit `c0ede7fcd50340170bc7f428b7b658f6e767a34c` with two workers. Each mode ran three times. All six runs passed.

| Mode | Wall times | Median |
| --- | --- | ---: |
| Plain pytest | 2.273 s, 2.277 s, 2.153 s | 2.27 s |
| LDR capture and SQLite storage | 3.908 s, 3.747 s, 3.830 s | 3.83 s |

The recorded runs each reported 36 project calls from two workers and marked the capture as truncated. The SQLite runtime was 3.50.4, so these runs used rollback journaling. This measures end-to-end overhead for the selected tests. The full upstream suite completed without recording and its recorded run was stopped before completion.

```bash
python -m pytest tests/test_recipes.py -n 2 -k 'TakeTests or TailTests or ConsumeTests or NthTests or AllEqualTests' -q -p no:cacheprovider -p ldr.pytest_worker
python -m ldr pytest tests/test_recipes.py -- -n 2 -k 'TakeTests or TailTests or ConsumeTests or NthTests or AllEqualTests'
```

After adding live SQLite streaming, one paired run of the same selection took
4.55 s plain and 8.49 s recorded. Both passed. The recorder stored all 64 scoped
calls from the two workers. Thirty-seven calls had at least one value that
could not be compared by the rolling fingerprints. This is a single pair of
measurements on the selected tests.

## Line budget audit

**24 KiB (24,576 bytes) matched full-trace line hits in this pilot.** It rounds up the observed cutoff of 23,495 serialized bytes, which retained an executed old patched line in all 9 of the 12 reproduced cases where the full completed-call trace contained one. The smallest measured cutoff for a majority was 16,381 bytes, with 7 of 12 hits. Four of the 16 attempted cases were excluded, and seven of the nine hits came from tqdm.

Shimari et al. studied 831 real Java bugs from Defects4J. Keeping the latest 1,024 events per instrumented location retained all observed bug-affected states in 84.3% of cases. Total storage grows with the number of locations. The study measures a per-location retention policy, while the pilot measures one serialized byte budget shared by an entire run. [Published study](https://doi.org/10.1016/j.scico.2024.103117) · [Buffer methodology](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4598613)

| Case | Completed calls | First hit, bytes | All observed hits, bytes | Full completed trace, bytes |
| --- | ---: | ---: | ---: | ---: |
| PySnooper/2 | 1 | — | — | 380 |
| PySnooper/3 | 7 | — | — | 4,660 |
| cookiecutter/2 | 30 | 3,319 | 16,353 | 19,608 |
| cookiecutter/3 | 12 | 936 | 936 | 10,399 |
| cookiecutter/4 | 6 | — | — | 3,256 |
| tqdm/1 | 98 | 23,495 | 23,495 | 60,267 |
| tqdm/2 | 9 | 5,533 | 5,533 | 5,994 |
| tqdm/4 | 30 | 12,829 | 12,829 | 18,237 |
| tqdm/5 | 367 | 16,381 | 70,556 | 207,400 |
| tqdm/6 | 14 | 9,777 | 9,777 | 10,680 |
| tqdm/8 | 23 | 18,231 | 18,231 | 18,781 |
| tqdm/9 | 39 | 923 | 24,067 | 24,413 |

The audit uses all listed BugsInPy cases from the three chosen repositories. It requires the upstream regression test to fail on the buggy version, pass on the fixed version, and still fail with recording. The same fixed test files and helpers are used on both versions. PySnooper/1 and cookiecutter/1 passed on both versions on this host. tqdm/7 failed on both. tqdm/3 had no eligible deleted or replaced Python line in the old production code. Four measured tqdm cases had one unfinished call each.

A hit is a retained call that executed a production line deleted or replaced by the fix. A hit is evidence that the patched code was reached. For each case, the script sums the UTF-8 bytes of persisted call payloads and frame IDs in completion order up through the first such call. The original pilot used a 64 MiB audit allowance. The current audit reads the completed trace directly. The thresholds use recorded prefix sizes, which can vary slightly on rerun because payloads include timings. This audit excludes SQLite page overhead and insertion-only fault locations. [BugsInPy revision](https://github.com/soarsmu/BugsInPy/tree/11c5f1eea954a42132cfd06bf257766a7963e0fd) · [Patch-based fault-location methodology](https://link.springer.com/article/10.1007/s10664-024-10475-3)

A separate check of the newer Hypothesis regression #4532 reproduced the failure and the fix using pytest 8.4.2. Its patched line was retained at 2,422 bytes in a trace of 19 completed calls and 11,992 bytes. It is outside the cohort used to calculate the pilot thresholds. [Upstream fix](https://github.com/HypothesisWorks/hypothesis/commit/717d957195568256a5bd4ac0d60a49c93b77fe0f)

To rerun the cohort, place the BugsInPy checkout and clones of PySnooper, cookiecutter, and tqdm in one directory, using those exact clone names. The measured environment was Python 3.13.17, pytest 9.2.0, pytest-mock 3.17.0, nose 1.3.7, and the subjects' test dependencies. The compatibility directory restored the removed `collections` ABC aliases, supplied `imp.py` from CPython v3.11.9, and provided the `_ERR_MSG` and `_TextTestResult` aliases those old test dependencies import. Pass the same directory as `--deps` for both revisions. The script prints each case and the final threshold calculation as JSON.

```bash
# Set PYTEST_PLUGINS=pytest_mock in the environment.
python evaluation/audit_line_budget.py /path/to/audit-checkouts --deps /path/to/compat-deps --timeout 30
```

## Scaled rich fixture, lookup cost and repeated walks

These tools measure what the fingerprint walk and the previous-run lookup cost. They are for LDR's developers.

- `overhead.py --only rich-scaled` runs a fixture that has the object types, sizes, roles and call counts of the rich run at 1/100 (`rich_scaled.py`, `rich_scaled_spec.json`). The spec comes from one recorded rich run and the rows of the walk probe; `python evaluation/rich_scaled.py extract ...` rewrites it. The fixture does not copy the work rich does between calls or who calls whom; see the docstring of `rich_scaled.py`.
- `lookup_cost.py --before CHECKOUT --after CHECKOUT` times two checkouts of LDR in one session, each with a previous run to compare with, and times the comparison from inside (`lookup_probe/`).
- `walk_probe/run_probe.py NAME PREFIX` records one run with a probe on the fingerprint walk (the walked object's identity, digest, node count and time, and what its nodes were made of). `repeated_walks.py PREFIX` counts the walks that read an object an earlier walk had already read, unchanged, and what share of the nodes and time they are.
