# Changelog

## Unreleased

- Every run is recorded and compared with the previous run of the same target, through SQLite. A pytest run is matched by test node id; other runs by exact command. The last 2 runs of each target are kept.
- Removed: `verify`, `--golden`, `--diff`, `--snapshot-name`, `--learn-volatility` and the `.ldr/snapshots/` folder. A difference between two runs of identical files carries "also varied across 2 runs with no edit".
- Added a size counter: every value the fingerprint walk reads leaves its type, role, call address and node count in the `value_sizes` table.

## 0.6.0

**Breaking: snapshots recorded by 0.5.x cannot be compared.** Trace schema 7
replaces schema 6. Comparing against a schema 6 snapshot answers `unknown` and
tells you to re-record it (`--golden`). Nothing is silently trusted.

Comparison reads the full value, not the preview:

- Calls carry a BLAKE2b fingerprint of the full value. A change at item 8 of 10
  no longer reads as unchanged. Previews are for reading only.
- Dict order is part of a value; set order is not. The receiver (`self`/`cls`)
  has a fingerprint, so the same arguments on an object in a different state are
  a different call. A receiver with unreadable parts (a lock, a socket) that
  matches in everything readable is reported as "equal in readable parts only",
  never as equal.
- Values past 10,000 nodes or depth 24 are `unknown`, reported as "cannot be
  compared". There is deliberately no time budget, so a fingerprint never depends
  on machine load.
- Recording never runs user code: no `__repr__`, `__str__` (for values),
  properties, `__getattr__`, `__getstate__`, `__hash__`, `__eq__`, `__iter__` or
  `__len__`. Custom objects preview as `ClassName(field=..., (+N more fields))`;
  built-in previews are unchanged. Sets and dicts above 1,000 entries keep their
  iteration order in previews instead of being sorted.

Secrets never reach disk or output (best effort, see the README):

- Environment values, known shapes (AWS, GitHub, Stripe, Slack, OpenAI/Anthropic,
  Google, JWT, PEM, URL credentials and query secrets, Bearer) and compound names
  are replaced by labeled placeholders such as `<secret: github-token, 40 chars>`.
  Redacted values keep no value and no hash; each run reports how many.
- Exception text and the crash summary are guarded, capped and redacted.
- Command-line credentials (`--password=...`, `--token ...`) are redacted in
  stored and printed arguments; the program still receives them.

Every run states its facts:

- `COMMIT:` (read-only git; LDR never writes to git), `SCOPE:` with the number of
  project calls recorded, a warning when there were none, and the outcome and
  commit of a saved baseline and of both sides of a comparison.
- Fields learned to vary between identical runs are listed as `UNSTABLE (shown,
  not compared)` instead of being masked silently.
- Until every call is recorded, output says "calls beyond the first N per
  function are not compared" whenever calls were sampled.

Also:

- The word-overlap ranking signals are removed; the replacement waits for a
  benchmark. Suggestion evidence no longer has the `*_overlap` keys.
- LDR refuses to start below Python 3.12 with a clear message.
- `--timeout` warns when it cannot be enforced (Windows has no `SIGALRM`).
- `python -m ldr` works; the volatility probe is now the noise run.
- In pytest mode, running a target under `tests/` or `test/` from a project
  directory without project markers can record calls to sibling source packages,
  including packages without `__init__.py`.

## 0.5.2

- Capture bounded before/after evidence when a mutable input keeps object
  identity but changes during a recorded call.
- Surface those caller-visible mutations directly beneath the responsible
  frame in the broad failed path and persist them in JSON.
- Rank observed input-mutating frames above adjacent read-only predicates when
  the failure follows the mutation.

## 0.5.1

- Rank up to three observed frames for focused agent follow-up using explicit,
  single-run evidence signals and conservative noise penalties.
- Add `ldr inspect --frame-id N`, which restores and reruns the latest target
  without requiring agents to reconstruct long selectors and source scopes.
- Detect common `src/`, setuptools, Poetry, Hatch, and flat-package layouts;
  warn when an explicit scope misses production code.
- Report execution, post-processing, and total LDR wall time in text and JSON.
- Preserve more value detail during bounded focused inspection and hide
  low-signal line steps so decisive transitions survive the prose budget.
- Make text and JSON agree when repetition sampling makes a capture incomplete.
- Preserve the explicit portable `--frame` and wildcard selector workflow.
