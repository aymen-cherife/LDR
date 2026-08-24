---
name: ldr-debug-runtime
description: Diagnose reproducible Python test failures and behavioral regressions with LDR runtime evidence. Use when a coding agent can run the failing code but the traceback, assertion, or static reading does not reveal the responsible runtime value or function. Do not use for syntax, import, dependency-installation, or build failures that prevent Python execution.
---

# LDR runtime debugging

Reproduce the failure normally before using LDR. Keep the target to one failing test or minimal script.

Run a bounded evidence pass:

```bash
ldr pytest tests/test_feature.py::test_case --only src tests
```

Read `.ldr/last.json`. Prioritize `failures`, `recent_calls`, and `frame_index`; treat a test assertion as the symptom unless application state proves otherwise. Form a runtime hypothesis before editing source.

When the broad pass identifies a suspicious successful call, copy its `frame_id` and inspect that exact invocation:

```bash
ldr inspect tests/test_feature.py::test_case \
  --frame 'src/feature.py:42:calculate#1' \
  --only src tests
```

Use the ordered state transitions to identify the statement that first violates the expected invariant. If the frame is missed, rerun the broad trace and select a fresh id; execution order changed.

If a repeated function may behave differently across invocations, use its
`frame_index.functions[].inspect_selector` to inspect every invocation in one
focused rerun:

```bash
ldr inspect tests/test_feature.py::test_case \
  --frame 'src/feature.py:42:calculate#*' \
  --only src tests
```

Look for the first invocation whose inputs, nested boolean state, callee
results, or ordered state transitions violate the expected invariant. Broad
traces sample repetitive calls to protect useful late evidence; the JSON status
reports both sampling and hard-budget overflow explicitly.

For a behavioral regression, save the pre-edit behavior and compare after the patch:

```bash
ldr verify tests/test_feature.py::test_case --only src tests
# edit
ldr verify tests/test_feature.py::test_case --only src tests
```

Preserve LDR's exit code: a failing target still exits nonzero even when evidence was written successfully. Do not repeatedly trace the whole suite, enable `--full` by default, or inspect frames without a concrete hypothesis. Verify the final patch with the project's normal tests, not LDR alone.
