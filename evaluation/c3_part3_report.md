# C3 part 3: incomplete

Gate 2 is blocked: the timer-free Python attrs reference was killed by SIGKILL. Step 4 was not run. There is no end-to-end recording speedup claim.

CI fixes: Ubuntu required unavailable history in a shallow checkout. Windows had that failure plus an unconditional Unix resource import. Both causes were corrected without CI edits or test skips.

The native recording contract, exact ceiling/sketch/status handling, strong fallback ownership, per-thread and re-entrant readers, automatic version/GIL/watcher/self-check selection, and three-field disclosure are implemented. Layout cache removals and allocations occur outside its mutex. Bulk containers retain native traversal and delegate individual scalar encodings. Python status constants are preserved, including marshal interning.

Targeted Gate 1: **149 passed, 1 Windows-only skip on each of CPython 3.12.14 and 3.13.5**. Seeds 0, 1 and random passed. Each version checked 23,245 fingerprint values / 2,617,850 positions (none missing), plus 180 subprocess values / 10,825 nodes (no mismatches). A source tree without the extension produced 10 passed / 67 skips, all native skips saying `native module not built`. No full suite was run.

Final-code live Rich walks (one measured replay per reader, same retained references; separate byte/counter validation before timing):

| Python | Values | Nodes | Python ns/node | Native ns/node | Python ThemeStack s | Native ThemeStack s | Delegated nodes | Native share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 3.12.14 | 300,848 | 12,219,504 | 2235.6 | 229.5 | 15.401988 | 0.681116 | 213,050 | 98.2565% |
| 3.13.5 | 300,581 | 12,209,728 | 2376.5 | 234.2 | 16.319632 | 0.655989 | 212,882 | 98.2565% |

Both byte/counter checks passed for every retained value; keeping references changed no Rich test outcomes. Corpora differ across interpreter versions. Timings are walk-only and include native delegation; they are not the requested four recording runs.

Conditions: Linux x86_64, kernel 6.18.57, glibc 2.41, Rich 15.0.0 real nine-file subset, pytest 8.4.2, PYTHONHASHSEED=0. Portable release, fat LTO, codegen-units=1, panic unwinding, per-Python ABI, no CPU-specific flags. The 3.13 corpus used the 3.13 interpreter with the same pinned fixture dependencies.

machine: ok - 0 run(s) alive, disk 58.0 GB free (92.1%), runs stored 0 / keep_runs 2, CPU 0% busy, python start 0.02 s, Python 3.12.14, reader native, LDR 0.6.0, native 0.1.0, Python build normal, venv yes, check 1.029 s wall, 0.006 s CPU

Toolz: first and edited second runs under each reader had identical raw stored evidence after explicitly separating required disclosure fields. Verdicts each: 6 same, 1 changed, 1 new, 1 missing, 4 not compared. The initial comparison exposed an interned-status-string marshal-tag difference; restoring the Python status constants corrected it.

Attrs: the first Python reference hit the inherited 300-second timeout. After disabling that existing timeout, the Python program was killed with exit -9; last progress was 802 seconds / 53 calls at _ClassBuilder._eval_snippets / _generate_unique_filename. The cgroup peaked at its 32 GiB limit and reported one OOM kill. This strongly supports OOM, but dmesg was denied, so PID-specific kernel attribution and the exact allocating function are unconfirmed. Other targeted checks/builds ran during this correctness attempt; no isolated memory attribution is claimed. Native attrs was never attempted. No data was cut to work around it.

**Four-run table: not run because Gate 2 did not pass.** Rich stored-signature/value_sizes/verdict equivalence under both recording readers remains unverified.

Full delegated-by-type table (no types omitted):

| Type | CPython 3.12.14 | CPython 3.13.5 |
|---|---:|---:|
| `builtins.str` | 57,758 | 57,762 |
| `_thread.RLock` | 37,919 | 37,823 |
| `builtins.builtin_function_or_method` | 27,137 | 27,137 |
| `_io.StringIO` | 19,818 | 19,746 |
| `builtins.type` | 17,174 | 17,172 |
| `rich.highlighter.ReprHighlighter` | 13,677 | 13,677 |
| `builtins.list_iterator` | 13,660 | 13,660 |
| `rich.console.ConsoleThreadLocals` | 12,392 | 12,392 |
| `builtins.generator` | 3,426 | 3,424 |
| `builtins.builtin_method` | 2,296 | 2,296 |
| `abc.ABCMeta` | 2,010 | 2,010 |
| `builtins.str_iterator` | 1,682 | 1,682 |
| `re.Pattern` | 901 | 901 |
| `builtins.function` | 830 | 829 |
| `_pytest.capture.EncodedFile` | 716 | 716 |
| `builtins.bytes` | 552 | 552 |
| `collections.deque` | 230 | 230 |
| `builtins.zip` | 100 | 100 |
| `builtins.enumerate` | 98 | 98 |
| `builtins.tuple_iterator` | 76 | 75 |
| `unittest.mock._Call` | 68 | 68 |
| `builtins.dict_itemiterator` | 42 | 42 |
| `tests.test_pretty.test_user_dict.<locals>.D2` | 36 | 36 |
| `unittest.mock._CallList` | 36 | 36 |
| `tests.test_pretty.test_user_dict.<locals>.D1` | 34 | 34 |
| `builtins.callable_iterator` | 32 | 32 |
| `tests.test_pretty.test_broken_getattr.<locals>.BrokenAttr` | 28 | 28 |
| `collections.defaultdict` | 26 | 26 |
| `_io.BytesIO` | 25 | 25 |
| `array.array` | 24 | 24 |
| `re.Match` | 24 | 24 |
| `rich.pager.SystemPager` | 24 | 24 |
| `tests.test_pretty.test_lying_attribute.<locals>.Foo` | 24 | 24 |
| `_io.TextIOWrapper` | 23 | 23 |
| `_pytest.capture.CaptureIO` | 20 | 20 |
| `builtins.dict_items` | 16 | 16 |
| `builtins.AttributeError` | 12 | 12 |
| `builtins.slice` | 12 | 12 |
| `unittest.mock.MagicMock` | 12 | 12 |
| `builtins.mappingproxy` | 11 | 11 |
| `rich.highlighter.JSONHighlighter` | 10 | 10 |
| `typing._ProtocolMeta` | 10 | 10 |
| `builtins.NotImplementedType` | 7 | 7 |
| `datetime.datetime` | 7 | 7 |
| `itertools.filterfalse` | 7 | 7 |
| `rich.live.Live` | 5 | 5 |
| `_pytest.fixtures.SubRequest` | 4 | 4 |
| `_pytest.python.Function` | 4 | 4 |
| `_thread.lock` | 4 | 4 |
| `builtins.dict` | 4 | 4 |
| `builtins.str_ascii_iterator` | 4 | 4 |
| `builtins.FrameLocalsProxy` | 0 | 2 |
| `rich.highlighter.NullHighlighter` | 2 | 2 |
| `builtins.filter` | 1 | 1 |

Deviations and remaining work:

- Gate 2 attrs and the four-run recording proof remain incomplete.
- The first attrs attempt inherited the existing timeout. The harness was fixed to use the existing --timeout=0 option; no new production flag was added.
- An added natural-GC scheduling test stalled on 3.13 and was interrupted after 191 seconds, then replaced by deterministic test-only GC scheduling. No recording cap or threshold was added.
- Several additional safety/coverage tests were added after the relevant implementation; not every added test was demonstrated red on the old code.
- The actual result file is .ldr/last.json; no second result.json file was introduced.
- No full suite, CI wait, wheel rollout, Windows native build or perf use, as instructed.

Exact raw benchmark results, source content hashes, memory evidence and conditions are in [c3_part3_results.json](c3_part3_results.json).
