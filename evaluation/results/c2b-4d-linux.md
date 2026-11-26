# C2b step 4d on Linux

The throughput objective failed. In the single controlled pair, Python took 45.349 s and native took 50.713 s. Native was 11.83% slower. Functional checks are reported separately below.

## Step 1, accepted diagnostic baseline

Raw walk time W was 22.574665 s within P = 35.836 s after the first committed batch. Share was 62.99%. The ideal bound for that interval is 2.702x. With total wall T = 43.143 s, removing measured W gives a total-wall bound of 2.098x.

Calibration for 300,860 empty timer pairs was 0.046360 s, reported separately. The first diagnostic attempt executed the fixture but failed phase-file collection because its temporary sitecustomize displaced the phase probe. These accepted figures came from the corrected second run. Timers wrapped _Walk.encode and _sketch, capturing walk hash updates and sketch hashing. Per-value hash construction and final hexdigest outside encode were not separately timed.

## Implementation

- 2a, 5b05c2b. Rust visited counts, flags, partial status, address-to-active-depth map and hash state. Python hash adapters exist at fallback boundaries. Failure cleanup restores the initial active path. The targeted suite passed 71 tests.
- 2b, 749f4f4. One native capture driver per entry or exit phase. Python generator continuations preserve caches, per-value accounting and the original order of return capture, mutable evidence, and exit arguments. Rust invokes the walk internally for each requested value. Python orchestration still resumes between values. The batching regression first observed two entries, then passed.
- 2c, 6670b7d. Direct indexed list/tuple reads and PyDict_Next iteration, with dictionary mutation checks. Subclasses retain the structural path. A structural test first failed on iterator construction.
- 2d, 4960397. Reuse Rust descriptor plans per uninterrupted walk segment. Existing weak Python per-class readers validate names, slots, descriptors and bases at first use, including class edits. Fallback invalidates the segment plans. Plans retain descriptor references only for the segment, not in a process-wide class cache.
- 2e, 6607501. Interpreter references are cached by identity and validated outside node traversal, refreshed after fallback. Native secret-name matching avoids the per-field Python helper. A fallback redaction-policy replacement regression failed first and then passed. Reference validation still runs at each value walk; attribute-name interning is not implemented for every method call.
- 2f, f9bf472. Homogeneous int, float and bool bulk encoding restores exact Python node accounting, including overflow. The new regression first observed different visited counts and then passed. String bulk redaction order is retained.
- 2g, 0d4de39. Combine field-name headers, object headers and sorted set digests at natural boundaries. Existing parity tests passed before and after this change; no failing update-count test was added.
- 2h, fee23ea. Reuse traversal-stack and numeric scratch-buffer capacity across values in a capture phase and remove scalar integer/float Rust string copies. Object-field snapshots and additional allocations such as copied identities and formatted headers remain. This reduces avoidable allocations but does not eliminate all avoidable per-node allocation.

Class plan construction and validation still cross Python, and the phase driver still resumes Python cache/accounting continuations. Reference validation and Python C-API operations remain. These residual costs are visible in the implementation. Their individual time contributions were not measured, so they are possible explanations rather than a measured attribution of the regression.

## Controlled Rich comparison

Linux 6.18.57 x86_64, glibc 2.41, CPython 3.12.14, Rich 15.0.0, the real nine-file subset, default mode, seed 0. New source commit fee23ea. Both new readers used the same prepared previous recording at the same fixture path, Python first and native second, once each. The new timed runs had the diagnostic reason/type probe off. The historical step 4c native run included that probe, so cross-series differences include instrumentation and session effects.

Portable release build with lto="fat", codegen-units=1 and panic unwinding. Build options and Cargo configuration were checked for CPU-specific flags. The measured extension SHA256 is d1d07189247df3d33731f6757fbca8c4f114323302e3818cc1a5e519db32b0da.

| Measure | Step 4c Python | Step 4c native | Step 4d Python | Step 4d native |
|---|---:|---:|---:|---:|
| wall (s) | 41.703 | 64.413 | 45.349 | 50.713 |
| python_start (s) | 0.039 | 0.033 | 0.043 | 0.029 |
| to_first_committed_batch (s) | 1.420 | 1.280 | 1.520 | 1.310 |
| program_after_first_batch (s) | 34.568 | 57.681 | 34.555 | 39.699 |
| call_records_move_with_indexes (s) | 0.857 | 0.881 | 1.828 | 1.780 |
| fingerprint_lookup_move_with_index (s) | 1.115 | 0.757 | 1.333 | 1.430 |
| report_ordered_scan (s) | 0.847 | 0.872 | 0.875 | 1.006 |
| report_grouped_counts (s) | 0.079 | 0.084 | 0.086 | 0.087 |
| writer_wait (s) | 0.012 | 0.014 | 0.013 | 0.016 |
| result_serialization_and_write (s) | 0.017 | 0.019 | 0.016 | 0.020 |
| output_drain (s) | 0.000 | 0.000 | 0.000 | 0.000 |
| comparison_temp_index (s) | 0.000 | 0.000 | 0.000 | 0.000 |
| comparison (s) | 0.199 | 0.354 | 0.211 | 0.444 |
| untimed post-run remainder (s) | 2.531 | 2.391 | 4.843 | 4.847 |
| pruning (s) | 0.003 | 0.003 | 0.003 | 0.003 |
| return (s) | 0.015 | 0.044 | 0.023 | 0.042 |
| peak_rss (MiB) | 340.762 | 340.734 | 340.812 | 340.918 |
| supervisor_peak_rss (MiB) | 415.855 | 417.730 | 414.684 | 415.289 |
| Native nodes delegated to Python | — | 60,210 | — | 60,210 |

The new native run accounted for 11,654,524 walked nodes, matching the previous 11,654,524. The timed native run retained the existing fallback counter without the external reason/type probe.

## Equivalence and tests

same_records against 3976cd4 exited 0 for orders and toolz. Signatures, stored record content, value_sizes and verdicts matched. DUMP_DROP=python_nodes,native_reader excluded the added developer telemetry as in the prior series. Toolz reported an environment difference of 21 versus 22 packages. No stored record differences were printed.

| Fixture, both versions | Same | Changed | New | Missing | Not compared |
|---|---:|---:|---:|---:|---:|
| orders | 20,004 | 1 | 1 | 1 | 5 |
| toolz | 6 | 1 | 1 | 1 | 4 |

The parity probe was extended to cover the new batched capture path and its subprocesses. That affected test passed. The final full suite was run once, alone, with the measured native module loaded. Command `.venv/bin/python -m pytest -q` completed with `470 passed, 8 skipped in 388.66s (0:06:28)`.

## Qualifications

The speed success criterion failed. The implementation also leaves incomplete portions of the strict lookup-only-after-change, intern-all-names and remove-all-avoidable-allocations goals described above. The Python continuation creates an unused Python hash object per value before the native walk, and object identities and formatted headers still allocate copies. Layout plans reset at explicit node fallback boundaries; Python metadata-validation helpers remain callbacks, so the stronger callback-free-segment invalidation guarantee has not been established for every possible mutation path. Passing parity tests and record comparisons demonstrate the exercised cases, rather than a proof for every possible callback. No extra optimization or timed rerun was attempted after the result. Production packaging is deferred.
