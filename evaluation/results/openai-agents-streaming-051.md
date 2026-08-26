# LDR 0.5.1 real-regression validation

Target: `openai/openai-agents-python` at
`6904dcbbdbf211aa2542a1e824db28985a0e914b`, with the focused streaming test
from `0edeffcfab2e26c4a48409537d8a0bb99b2a76f6` applied as an untracked fixture.

Failure:

```text
tests/test_streaming_tool_call_arguments.py::test_streaming_tool_call_arguments_not_empty
AssertionError: Tool call arguments should not be empty, got: ''
```

## Broad pass

Command (no manual source scope):

```bash
ldr pytest tests/test_streaming_tool_call_arguments.py::test_streaming_tool_call_arguments_not_empty
```

LDR detected `src` and `tests`. Its ranked shortlist was:

1. `AgentRunner._run_single_turn_streamed` — 75.32, all two calls;
2. `AgentRunner.run_streamed` — 74.90;
3. `RunImpl.execute_function_tool_calls.<locals>.run_single_tool` — 73.79.

The first candidate is the runtime decision frame identified manually in the
earlier 0.5.0 evaluation. LDR 0.5.1 selected it without a hand-built selector.
The trace recorded 242 nodes, sampled six repetitive calls, overflowed zero
paths, and reported `trace_truncated=true` consistently in text and JSON.

Measured by LDR: execution 1.643 seconds, post-processing 0.026 seconds, total
through report construction 1.671 seconds.

## Focused pass

Command:

```bash
ldr inspect --frame-id 172
```

The short id restored the runner, target, project root, `src tests` scope, and
the wildcard selector. Both recorded turns matched. The focused state history
directly preserved:

- `ResponseOutputItemAddedEvent` with `arguments=''`;
- `output_item` and the constructed `ToolCallItem` carrying that empty value;
- `emitted_tool_call_ids: set() -> {'call_123'}`;
- the subsequent `ResponseOutputItemDoneEvent` carrying
  `arguments='{"a": 5, "b": 3}'`.

Measured by LDR: execution 1.531 seconds, post-processing 0.001 seconds, total
through report construction 1.535 seconds.

## Boundary

This validates source-scope recovery, ranking, shortcut restoration, wildcard
inspection, focused value fidelity, and timing on a real repository. It is not
a blind repair result: the fixture name and assertion describe the symptom.
No production code or test was edited during the validation, and no fix was
applied.
