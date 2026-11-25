"""Measure named post-run steps in either checkout without modifying it."""

import atexit
import functools
import json
import os
import sys
import threading
from pathlib import Path
from time import perf_counter

output = os.environ.get("LDR_PHASE_OUTPUT")
program = os.environ.get("LDR_RUN_CHILD") == "1"
supervisor = "ldr.supervisor" in sys.orig_argv and "--run" in sys.orig_argv
timings = {}


def record(name, start):
    timings[name] = timings.get(name, 0) + perf_counter() - start


def measure(owner, method, name):
    original = getattr(owner, method)

    @functools.wraps(original)
    def timed(*args, **kwargs):
        start = perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            record(name, start)

    setattr(owner, method, timed)


def peak_rss_bytes():
    if os.name != "nt":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024

    import ctypes
    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    info.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
    info.restype = wintypes.BOOL
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not info(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.PeakWorkingSetSize


def save():
    role = "program" if program else "supervisor"
    row = {"seconds": timings, "python": sys.version.split()[0]}
    if program:
        row["peak_rss_bytes"] = peak_rss_bytes()
    Path(output, f"{role}-{os.getpid()}.json").write_text(json.dumps(row), encoding="utf-8")


def instrument_writer():
    import sqlite3
    from ldr.sqlite_store import TraceStore

    connect = sqlite3.connect
    build = TraceStore.build_lookup

    class TimingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            statement = " ".join(sql.split())
            if statement.startswith("INSERT INTO call_records SELECT"):
                name = "call_records_move_with_indexes"
            elif statement.startswith("INSERT INTO call_lookup SELECT"):
                name = "fingerprint_lookup_move_with_index"
            else:
                return super().execute(sql, *args, **kwargs)
            started = perf_counter()
            try:
                return super().execute(sql, *args, **kwargs)
            finally:
                record(name, started)

    def connect_timed(*args, **kwargs):
        return connect(*args, factory=TimingConnection, **kwargs)

    def build_timed(store):
        sqlite3.connect = connect_timed
        try:
            return build(store)
        finally:
            sqlite3.connect = connect

    TraceStore.build_lookup = build_timed

    create, join = threading.Thread.__init__, threading.Thread.join

    def tracked_thread(thread, *args, **kwargs):
        target = kwargs.get("target")
        thread.ldr_output_pump = getattr(target, "__name__", "") == "_pump"
        create(thread, *args, **kwargs)

    def timed_join(thread, *args, **kwargs):
        if not getattr(thread, "ldr_output_pump", False):
            return join(thread, *args, **kwargs)
        started = perf_counter()
        try:
            return join(thread, *args, **kwargs)
        finally:
            record("output_drain", started)

    threading.Thread.__init__, threading.Thread.join = tracked_thread, timed_join

    replace = os.replace

    def publish(source, destination, *args, **kwargs):
        if os.path.basename(destination) == "state.json":
            state = json.loads(Path(source).read_text(encoding="utf-8"))
            if state.get("state") == "finished":
                save()
        return replace(source, destination, *args, **kwargs)

    os.replace = publish


if output and program:
    import sqlite3
    from ldr import sqlreport, store, summary

    connect = sqlite3.connect

    class ComparisonConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if not sql.startswith("CREATE INDEX temp.now_frame"):
                return super().execute(sql, *args, **kwargs)
            started = perf_counter()
            try:
                return super().execute(sql, *args, **kwargs)
            finally:
                record("comparison_temp_index", started)

    def connect_comparison(*args, **kwargs):
        return connect(*args, factory=ComparisonConnection, **kwargs)

    sqlite3.connect = connect_comparison
    measure(sqlreport, "frame_index", "report_ordered_scan")
    measure(sqlreport, "counts", "report_grouped_counts")
    measure(summary, "summarize", "result_serialization_and_write")
    measure(store, "atomic_text", "result_serialization_and_write")
    measure(store, "atomic_json", "result_serialization_and_write")
    atexit.register(save)
elif output and supervisor:
    instrument_writer()
