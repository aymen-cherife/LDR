"""Timers around the recording path, installed only in LDR's child process.

Started by evaluation/cost_by_part.py through PYTHONPATH. Writes its totals to the file
named by LDR_COST_OUT when the child exits. Measurement only; not part of LDR.
"""

import atexit
import json
import os
import time

if os.environ.get("LDR_RUN_CHILD") == "1" and os.environ.get("LDR_COST_OUT"):
    TOTAL, COUNT = {}, {}
    clock = time.perf_counter

    def wrap(owner, name, label):
        original = getattr(owner, name)

        def timed(*args, **kwargs):
            started = clock()
            try:
                return original(*args, **kwargs)
            finally:
                TOTAL[label] = TOTAL.get(label, 0.0) + clock() - started
                COUNT[label] = COUNT.get(label, 0) + 1

        setattr(owner, name, timed)

    from ldr import call_stream, fingerprint, tracer

    for owner, name, label in (
        # Not _on_start/_on_return: they read sys._getframe(1), and a wrapper in between
        # would change which frame that is.
        (tracer.Recorder, "_next_frame_id", "  start: address (_next_frame_id)"),
        (tracer.Recorder, "_new_node", "  start: tree node (_new_node, first calls only)"),
        (tracer.Recorder, "_new_shadow", "  start: shadow node"),
        (tracer.Recorder, "_attach", "  start: attach node"),
        (tracer.Recorder, "_finish_timing", "  return: timing (_finish_timing)"),
        (tracer.Recorder, "_matching_stack", "  return: find stack entry"),
        (tracer.Recorder, "_start_capture", "  start: entry fingerprints + record start"),
        (tracer.Recorder, "_argument_fingerprints", "  fingerprint arguments (entry and exit)"),
        (tracer.Recorder, "_close", "  return: close (return value, exit, record)"),
        (call_stream.CallStream, "close", "    record close (signature, compare, parts)"),
        (call_stream.CallStream, "flush", "    flush (send batch)"),
        (call_stream, "call_signature", "      per-call signature"),
        (call_stream, "_encode", "      json encoding of a record"),
        (tracer, "fp_value", "    fingerprint of the return value"),
        (fingerprint, "_fingerprint_details", "      value walk (uncached)"),
    ):
        wrap(owner, name, label)

    def dump():
        calls = COUNT.get("  return: close (return value, exit, record)", 0) or 1
        rows = {
            label: {"seconds": round(TOTAL[label], 3), "calls": COUNT[label],
                    "us_per_recorded_call": round(TOTAL[label] / calls * 1e6, 1)}
            for label in TOTAL
        }
        with open(os.environ["LDR_COST_OUT"], "w", encoding="utf-8") as handle:
            json.dump({"recorded_calls": calls, "parts": rows}, handle, indent=1)

    atexit.register(dump)
