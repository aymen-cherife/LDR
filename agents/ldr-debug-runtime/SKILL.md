---
name: ldr-debug-runtime
description: Diagnose reproducible Python test failures and behavioral regressions with LDR runtime evidence. Use when a coding agent can run the failing code but the traceback, assertion, or static reading does not reveal the responsible runtime value or function. Do not use for syntax, import, dependency-installation, or build failures that prevent Python execution.
---

# LDR runtime debugging

Reproduce the failure normally before using LDR. Keep the target to one failing test or minimal script.

Run a bounded evidence pass:

```bash
ldr pytest tests/test_feature.py::test_case
```

Read `.ldr/last.json`. Prioritize `failures`, `recent_calls`, and
`suggestions`; treat a test assertion as the symptom unless application state
proves otherwise. A suggestion is an inspection target, not a claimed defect.
Check its `evidence` fields and form a runtime hypothesis before editing source.
If `mutated_inputs` is present, compare its bounded before/after evidence in
the failed path before spending a rerun on an adjacent read-only helper.

Inspect the strongest evidence-backed candidate with its short id:

```bash
ldr inspect --frame-id 37
```

LDR restores the prior runner, target, arguments, source scope, project root,
and portable frame selector. Use the ordered state transitions to identify the
first observed statement where an expected invariant becomes false. If the
frame is missed, rerun the broad trace because execution order changed.

If automatic scope detection warns that no production package was captured,
fix the scope before interpreting the rankings. Do not accept test-only
evidence as production localization.

If a repeated function may behave differently across invocations, use its
`frame_index.functions[].inspect_selector` to inspect every invocation in one
focused rerun:

```bash
ldr inspect tests/test_feature.py::test_case \
  --frame 'src/feature.py:calculate#*' \
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
