# C2b native coverage on Linux

Measured on Linux 6.18.57 x86_64, glibc 2.41, Python 3.12.14. Default mode, Rich 15.0.0, the real nine-file subset. Each controlled series prepared one previous recording and restored it at the same fixture path for Python first and native second, once each, with PYTHONHASHSEED=0.

## Step 4a

The external diagnostic probe at ee45170 visited 11,654,524 nodes and counted 3,942,989 delegated nodes. Repository code was unchanged for that diagnostic run.

| Delegation dispatch reason | Nodes |
|---|---:|
| structural/leaf fallback | 1,886,130 |
| text policy | 2,056,859 |

| Type, top 20 | Delegated nodes |
|---|---:|
| builtins.str | 2,056,859 |
| rich.style.Style | 496,046 |
| enum.EnumType | 296,188 |
| rich.color.ColorType | 293,114 |
| rich.color.Color | 293,102 |
| rich.text.Text | 76,732 |
| rich.segment.Segment | 67,952 |
| _thread.RLock | 37,465 |
| builtins.builtin_function_or_method | 26,477 |
| rich.console.Console | 25,419 |
| rich.console.ConsoleDimensions | 22,927 |
| rich.console.ConsoleOptions | 22,560 |
| rich.text.Span | 20,528 |
| _io.StringIO | 19,755 |
| rich.containers.Lines | 19,329 |
| rich.measure.Measurement | 17,278 |
| builtins.type | 17,174 |
| builtins.list_iterator | 13,660 |
| rich.highlighter.ReprHighlighter | 13,430 |
| rich._log_render.LogRender | 12,575 |

The native Children task takes each child yielded by the Python fallback and schedules a native Node task. Delegation is per node. Children return to native traversal.

The original implementation delegated every string and structurally read instance. It also transferred visited counts and mutable walk state through Python attributes per node. These are observed code paths. Their individual timing contributions were not measured.

## Step 4b

- f0e06be reads text and bytes in Rust, preserving existing redaction policies, surrogate encoding and temporary-path normalization through C-backed string/regex operations.
- cebafbb reads safe instance dictionaries, inherited/mangled/unset slots and builtin-container subclass contents using C descriptors. Layouts are inspected on each read so class edits remain visible. Python attribute hooks and unknown descriptors fall back.
- 37279c9 reads builtin leaves, bound methods, partials and exception arguments. Fallback children continue through native tasks.

Coverage tests were added and observed failing before each implementation. The targeted native tests then passed.

## Step 4c

The new series uses 37279c9. The native measurement includes an external fallback reason/type probe, so its instrumentation overhead is included and was not measured independently. Step 4 did not include that probe. Both series used one run per reader and the same phase instrumentation.

| Measure | Step 4 Python | Step 4 native | Step 4c Python | Step 4c native |
|---|---:|---:|---:|---:|
| wall (s) | 42.366 | 47.757 | 41.703 | 64.413 |
| python_start (s) | 0.047 | 0.044 | 0.039 | 0.033 |
| to_first_committed_batch (s) | 1.530 | 1.270 | 1.420 | 1.280 |
| program_after_first_batch (s) | 36.381 | 41.734 | 34.568 | 57.681 |
| call_records_move_with_indexes (s) | 0.786 | 0.818 | 0.857 | 0.881 |
| fingerprint_lookup_move_with_index (s) | 0.765 | 0.801 | 1.115 | 0.757 |
| report_ordered_scan (s) | 0.540 | 0.672 | 0.847 | 0.872 |
| report_grouped_counts (s) | 0.079 | 0.084 | 0.079 | 0.084 |
| writer_wait (s) | 0.021 | 0.027 | 0.012 | 0.014 |
| result_serialization_and_write (s) | 0.016 | 0.017 | 0.017 | 0.019 |
| output_drain (s) | 0.000 | 0.000 | 0.000 | 0.000 |
| comparison_temp_index (s) | 0.000 | 0.000 | 0.000 | 0.000 |
| comparison (s) | 0.212 | 0.276 | 0.199 | 0.354 |
| untimed post-run remainder (s) | 1.952 | 1.990 | 2.531 | 2.391 |
| pruning (s) | 0.003 | 0.004 | 0.003 | 0.003 |
| return (s) | 0.034 | 0.019 | 0.015 | 0.044 |
| peak_rss (MiB) | 352.801 | 341.176 | 340.762 | 340.734 |
| supervisor_peak_rss (MiB) | 423.816 | 424.367 | 415.855 | 417.730 |
| Native nodes delegated to Python | — | 3,942,989 | — | 60,210 |

Both diagnostic runs visited 11,654,524 nodes. Delegation fell by 98.47%. Native wall time in step 4c was 54.5% higher than Python. Coverage improved while the measured native run remained slower. Repeated native layout inspection, module lookups and Python-object operations remain in the implementation; their individual cost is unmeasured.

| Remaining delegation reason | Nodes |
|---|---:|
| unknown builtin or unreadable state | 60,112 |
| Python attribute hook | 98 |

Preflight before step 4c reported no live LDR runs, 56.6 GB free disk and 1% CPU busy. Each measured run reported 18% CPU busy in its startup machine check.

same_records against 3976cd4 exited 0 for orders and toolz. Records, signatures, value_sizes and verdicts matched. As in step 4, DUMP_DROP=python_nodes,native_reader excluded only the newly added developer telemetry. Toolz reported 21 versus 22 environment packages. No record differences were reported in this series.

| Fixture, both versions | Same | Changed | New | Missing | Not compared |
|---|---:|---:|---:|---:|---:|
| orders | 20,004 | 1 | 1 | 1 | 5 |
| toolz | 6 | 1 | 1 | 1 | 4 |

Full suite, once at the end with native built: `462 passed, 8 skipped in 387.47s (0:06:27)`. Command: `.venv/bin/python -m pytest -q`.
