"""Where the time of one recorded call goes, by part (inclusive times, nested parts overlap).

    python evaluation/cost_by_part.py DIR -- <ldr arguments>

Runs `ldr` in this process (as the child would) with the main parts of the recording path
wrapped by timers, and prints one JSON line: for each part its total seconds, calls and
microseconds per recorded call. The timers cost something themselves (about 0.2 us each),
so the numbers are for ranking the parts, not for adding up. Measurement only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from ldr import call_stream, fingerprint, tracer  # noqa: E402

TOTAL = defaultdict(float)
COUNT = defaultdict(int)
clock = time.perf_counter


def wrap(owner, name, label):
    original = getattr(owner, name)

    def timed(*args, **kwargs):
        started = clock()
        try:
            return original(*args, **kwargs)
        finally:
            TOTAL[label] += clock() - started
            COUNT[label] += 1

    setattr(owner, name, timed)


def main():
    index = sys.argv.index("--")
    os.chdir(sys.argv[1])
    sys.path.insert(0, os.getcwd())
    Recorder, Stream = tracer.Recorder, call_stream.CallStream
    for owner, name, label in (
        (Recorder, "_on_start", "callback: start (everything)"),
        (Recorder, "_on_return", "callback: return (everything)"),
        (Recorder, "_argument_fingerprints", "fingerprint arguments (entry and exit)"),
        (Recorder, "_start_capture", "start capture (entry fingerprints, detail, record start)"),
        (Recorder, "_close", "close (return value, exit fingerprints, record)"),
        (Stream, "start", "record start"),
        (Stream, "close", "record close (signature, compare, parts, detail)"),
        (Stream, "flush", "flush (stage rows, send batch)"),
        (call_stream, "call_signature", "per-call signature (blake2b)"),
        (call_stream, "_encode", "json encoding of a record"),
        (tracer, "fp_value", "fingerprint of the return value"),
        (fingerprint, "_fingerprint_details", "value walk (uncached)"),
    ):
        wrap(owner, name, label)
    os.environ["LDR_RUN_CHILD"] = "1"
    from ldr.cli import main as ldr_main

    started = clock()
    try:
        ldr_main(sys.argv[index + 1:])
    finally:
        wall = clock() - started
        calls = COUNT["record start"] or 1
        rows = {
            label: {
                "seconds": round(TOTAL[label], 3),
                "calls": COUNT[label],
                "us_per_recorded_call": round(TOTAL[label] / calls * 1e6, 1),
            }
            for label in TOTAL
        }
        print(json.dumps({"wall_seconds": round(wall, 3), "recorded_calls": calls, "parts": rows}, indent=1),
              file=sys.stderr)


if __name__ == "__main__":
    main()
