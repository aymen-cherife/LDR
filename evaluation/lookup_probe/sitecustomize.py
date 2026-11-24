"""Times what the previous-run comparison costs a run, on the recording thread (developers only).

LDR_LOOKUP_OUT=path: at exit, writes JSON {"load_s", "compare_s", "compare_calls"} to path.pid.
"load_s": the time to read the previous run into memory (the checkout before f435065 only);
"compare_s": the time inside CallStream._compare (a dict lookup there, a SQLite point lookup after).
"""
import atexit
import json
import os
from time import perf_counter_ns

OUT = os.environ.get("LDR_LOOKUP_OUT")

if OUT:
    import ldr.call_stream as cs
    import ldr.sqlite_store as ss

    totals = {"load_ns": 0, "compare_ns": 0, "compare_calls": 0}
    original = cs.CallStream._compare

    def timed(self, *args):
        started = perf_counter_ns()
        try:
            return original(self, *args)
        finally:
            totals["compare_ns"] += perf_counter_ns() - started
            totals["compare_calls"] += 1

    cs.CallStream._compare = timed
    if hasattr(ss.TraceStore, "read_reference"):
        read = ss.TraceStore.read_reference

        def timed_read(self, *args, **kwargs):
            started = perf_counter_ns()
            try:
                return read(self, *args, **kwargs)
            finally:
                totals["load_ns"] += perf_counter_ns() - started

        ss.TraceStore.read_reference = timed_read

    def write():
        if totals["compare_calls"] or totals["load_ns"]:
            with open(f"{OUT}.{os.getpid()}", "w") as handle:
                json.dump({"load_s": totals["load_ns"] / 1e9, "compare_s": totals["compare_ns"] / 1e9,
                           "compare_calls": totals["compare_calls"]}, handle)

    atexit.register(write)
