# Hard random benchmark sample: 2026-08-24

Seed: `20260824`  
Source: `evaluation/tasks.json`  
Application fixes: not applied

The score is based only on whether LDR caught or materially helped catch the
known defect. A traceback or assertion that already states the fix earns no
credit.

| Instance | Initial grade | Final grade | Verdict |
| --- | --- | --- | --- |
| `matplotlib__matplotlib-30467` | Fumbled | Caught | Fixed LDR |
| `octodns__octodns-1298` | Missed | Missed | Correctly out of wedge |
| `a2aproject__a2a-python-443` | Missed | Missed | Correctly out of wedge |

## Matplotlib 30467

Target:

```text
lib/matplotlib/tests/test_axis.py::test_tick_not_in_layout[png]
```

The initial broad trace exhausted its budget on hot getters and buried the
earlier successful layout call. LDR now samples repeated calls, preserves novel
children of sampled parents, indexes every retained function, and offers a
wildcard focused selector.

The repaired workflow retained:

```text
lib/matplotlib/axis.py:1333:Axis._get_ticklabel_bboxes#*
```

The wildcard inspection matched 44 invocations and exposed invocation `#13`:

```text
label1._in_layout: True -> False
label1._visible: True
Artist.get_visible() -> True
```

The same frame returned a bounding box for that label, and its source predicate
checked only `get_visible()`. This points to the gold function and the decisive
runtime contradiction without applying the application patch. Grade:
**Caught**.

## OctoDNS 1298

Pytest reported that `OwnershipProcessor.should_replace` did not exist. The
gold change adds that public option and threads it into record replacement.
LDR showed the constructor call but added no information beyond the direct API
contract failure. Adding runtime machinery to restate an `AttributeError` would
not improve agent diagnosis. Grade: **Missed; do not use LDR for this class**.

## A2A Python 443

The mock assertion printed the full contract mismatch: expected
`get(task_id, context)`, actual `get(task_id)`. LDR showed the handler received
`context` but did not improve on the assertion's exact call arguments. The
defect is already self-diagnostic interaction wiring. Grade: **Missed; do not
use LDR for this class**.

## LDR regressions added

- Bind pytest's exact late report exception instead of promoting unrelated
  runtime attribute probes.
- Sample hot functions after eight calls while retaining power-of-two samples.
- Preserve novel successful descendants inside sampled hot parents.
- Expose all retained functions even when the first 500-frame index is full.
- Capture bounded nested boolean object state during focused inspection.
- Show direct calls made by an inspected frame.
- Inspect every invocation of a selected function with a stable `#*` selector.

Validation: `62 passed`; core CI contract: `16 passed`.
