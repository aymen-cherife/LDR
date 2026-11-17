# Inspection ranking in LDR 0.5.2

LDR's broad trace should answer a narrow agent question: **which observed
invocation is worth one focused rerun?** It must not pretend that one failing
execution proves where the defect is.

## Signals used

Each completed frame receives a deterministic score from evidence in the
current execution. When production frames exist, test-source candidates are
removed from the final shortlist:

1. whether the frame is structurally on the failed execution branch;
2. how recently it completed relative to the observed failure;
3. whether an unhandled exception was recorded in the frame;
4. whether a mutable argument kept its identity but changed representation
   during the call;
5. a penalty for highly repetitive functions;
6. a penalty for common diagnostic noise such as display/serialization helpers
   and tracing/logging setup.

Words shared between a function's name (or its captured values) and the error
text are **not** a signal. On a real probe that overlap ranked the actual
culprit last, so the signals were removed. The replacement ranking method waits
for Bench 0.

Calls of the same function are collapsed to their strongest invocation. LDR
returns at most three suggestions and omits candidates more than 30 points
behind the strongest one instead of filling the list with weak guesses. Every
JSON suggestion contains the component evidence, score, portable frame id, and
short id used by `ldr inspect --frame-id N`.

The weights are product heuristics, not learned probabilities. They are kept
small, explicit, deterministic, and covered by behavioral tests so benchmark
cases cannot silently become special rules.

The mutable-input signal is intentionally narrow. LDR records it only for
common built-in mutable containers whose object identity is unchanged from
entry to exit. This avoids calling a local parameter rebind a caller-visible
mutation. The evidence remains a bounded representation change, not a formal
alias analysis or proof of defect.

## Research basis and limits

- [Dynamic program slicing](https://doi.org/10.1145/93548.93576) motivates
  prioritizing statements and calls that actually executed for this input over
  every location that could theoretically matter. LDR 0.5.2 records call-tree
  and selected local-state evidence; it does **not** compute a formal dynamic
  dependence graph or claim to return a dynamic slice.
- [Cause transitions](https://doi.org/10.1145/1062455.1062522) motivates the
  focused rerun's ordered local-state transitions. The paper compares failing
  and passing states; LDR's single-run ranking is deliberately weaker and does
  not call its transitions causal.
- [Fault localization using textual similarities](https://arxiv.org/abs/1211.2858)
  motivated an earlier identifier-overlap signal. It is removed in 0.6 (see
  above); the reference is kept as history.
- [The Whyline](https://doi.org/10.1145/1368088.1368130) motivates grounding
  explanations in observed behavior instead of asking the agent to invent a
  source-level cause before inspecting runtime evidence.
- [Holmes](https://www.microsoft.com/en-us/research/publication/holmes-effective-statistical-debugging-via-efficient-path-profiling/)
  demonstrates the value of path correlations across several runs. LDR does
  not expose Ochiai-like or statistical suspiciousness because one failing run
  has no passing population from which such a score could honestly be derived.

## What the agent may conclude

The agent may say that a frame is a strong next inspection because LDR observed
it on the failed branch, near the failure, or mutating an input in place.
It may not say that LDR proved that frame contains the bug. A focused state
transition is direct evidence of a value change at a statement; the causal
interpretation still requires program semantics, source reading, and a normal
regression test.

## Next evidence upgrade

The next principled ranking upgrade is paired passing/failing evidence. Once
LDR can align multiple comparable runs, it can add path/value contrasts and
statistical signals without disguising a single execution heuristic as fault
localization science.
