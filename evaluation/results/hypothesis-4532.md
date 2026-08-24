# SWE-bench-Live smoke test: Hypothesis 4532

Date: 2026-08-24  
Instance: `HypothesisWorks__hypothesis-4532`  
Base commit: `34c02559e8d94dfbbffd07ceee9109505bc550cb`  
Test: `hypothesis-python/tests/cover/test_reflection.py::test_cache_key_size_is_bounded`

This is a deterministic product smoke test on the newest Python row exposed by the public SWE-bench-Live `full` split. The gold fix was already known, so this is not counted as an autonomous agent solve.

## Broad evidence pass

```bash
ldr pytest hypothesis-python/tests/cover/test_reflection.py::test_cache_key_size_is_bounded \
  --only hypothesis-python/src hypothesis-python/tests
```

Result: target exit `1`; no trace truncation; 26 successful calls. The failure packet identified the second `_function_key` invocation:

```text
test_cache_key_size_is_bounded() ! AttributeError: 'bytes' object has no attribute 'encode'
  _function_key(..., bounded_size=False) -> (...)
  _function_key(..., bounded_size=True) ! AttributeError: 'bytes' object has no attribute 'encode'
```

Selected frame:

```text
src/hypothesis/internal/lambda_sources.py:68:_function_key#2
```

## Focused state pass

```bash
ldr inspect hypothesis-python/tests/cover/test_reflection.py::test_cache_key_size_is_bounded \
  --frame 'src/hypothesis/internal/lambda_sources.py:68:_function_key#2' \
  --only hypothesis-python/src hypothesis-python/tests
```

The state history exposed the invalid transition without tracing every line in the repository:

```text
87 | consts_repr = hashlib.sha384(consts_repr.encode()).digest()
    consts_repr: "(None, 'cccc...')" -> b"..."
88 | if len(defaults_repr) > 48:
89 | defaults_repr = hashlib.sha384(consts_repr.encode()).digest()
```

That evidence supports the gold diagnosis: line 89 should hash `defaults_repr`, because `consts_repr` was already converted to bytes on line 87.

## Verification

After applying the one-line gold fix, the public test passed. `ldr verify` compared the fixed run with the failing snapshot and ranked this first:

```text
[CHANGED HERE] test_cache_key_size_is_bounded > _function_key
    err: AttributeError: 'bytes' object has no attribute 'encode' -> None
```

## Issue LDR found in itself

Before this run, LDR traced pytest collection. The broad directory scope exhausted the 5,000-node budget during Hypothesis imports and buried the target behind expected module `AttributeError`s. LDR now arms tracing at `pytest_runtest_protocol`, before setup and fixtures but after collection. On the same command, the trace changed from truncated with 9,251 successful calls to untruncated with 26.

This proves the broad → inspect → verify workflow on one real task. It does not establish an agent-level solve-rate improvement; the paired eight-task evaluation remains the gate for that claim.
