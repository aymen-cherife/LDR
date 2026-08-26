# Changelog

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
