# Haystack 8940: mutation-ranking before/after

Date: 2026-08-26  
Benchmark: `SWE-bench-Live/SWE-bench-Live`  
Instance: `deepset-ai__haystack-8940`  
Base commit: `db4f23771abb41e598ad65041c997d72d6268dd9`  
Target: `test/core/pipeline/test_pipeline_base.py::TestPipelineBase::test__write_component_outputs_dont_overwrite_with_no_output`

The public benchmark test patch was applied to the pinned base commit. The
gold production patch was not read until the initial diagnosis was complete.
No Haystack production fix was applied during this evaluation.

## Symptom

An existing socket input containing `"keep"` was replaced by Haystack's
`_NO_OUTPUT_PRODUCED` sentinel. The ordinary assertion exposed the corrupted
final value but not the production branch that wrote it.

## LDR 0.5.1 baseline

The broad run captured six nodes and was not truncated. It suggested:

1. `is_socket_lazy_variadic`, score `104.25`;
2. `PipelineBase._write_component_outputs`, score `104.0`.

Following the top suggestion cost a focused rerun and produced only a `False`
return with no state transition. Inspecting the second suggestion then exposed
the decisive transition at `haystack/core/pipeline/base.py:1120`:

```text
inputs: ... 'value': 'keep' ...
  -> ... 'value': <class 'haystack.core.component.types._empty'> ...
```

The baseline diagnosis matched the gold patch: the standard-socket writer
must retain an existing input when the new value is `_NO_OUTPUT_PRODUCED`.

Baseline LDR timings:

- broad: `0.838s` internal, `1.035s` shell wall time;
- wrong top inspection: `0.879s` internal, `1.059s` shell wall time;
- decisive inspection: `0.929s` internal, `1.153s` shell wall time;
- ranked-workflow wall time: `3.247s` over three LDR runs.

## Improvement derived from the miss

LDR now captures bounded before/after evidence when a built-in mutable
argument retains object identity but changes during a call. That evidence is:

- persisted as `Node.input_changes` in schema version 6;
- rendered directly below the frame in the broad failed path;
- exposed as `mutated_inputs` and `mutation_value_overlap` in suggestion JSON;
- rewarded by the deterministic inspection ranker.

The signal does not treat a local parameter rebind as caller-visible mutation
and does not claim formal alias or causal analysis.

## LDR 0.5.2 rerun

On the identical checkout and failing test, the broad result became:

```text
PipelineBase._write_component_outputs(...)
  INPUT MUTATION inputs: ... 'keep' ... -> ... _empty ...

SUGGESTED INSPECTIONS
1. PipelineBase._write_component_outputs
   Reason: caller-owned input changed in place during call: inputs
```

The responsible writer moved from rank 2 to rank 1. The read-only predicate
was omitted from the bounded shortlist. The single recommended focused rerun
again exposed line 1120 as the exact write.

Post-change timings:

- broad: `0.801s` internal, `1.025s` shell wall time;
- decisive top inspection: `0.848s` internal, `1.026s` shell wall time;
- ranked-workflow wall time: `2.051s` over two LDR runs.

This removed one investigation turn and reduced measured workflow wall time by
about 37% on this instance without increasing broad execution time. It is one
real benchmark result, not a general solve-rate claim.
