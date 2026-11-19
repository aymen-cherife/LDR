"""Measure SQLite trace storage with generated calls and independent writers."""

import argparse
import hashlib
import json
import sqlite3
import tracemalloc
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

SCHEMA = """
CREATE TABLE runs (id INTEGER PRIMARY KEY);
CREATE TABLE captured_values (
    fingerprint BLOB NOT NULL PRIMARY KEY,
    payload BLOB NOT NULL
){value_table_options};
CREATE TABLE calls (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    worker INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    parent_ordinal INTEGER,
    function TEXT NOT NULL,
    input_fingerprint BLOB NOT NULL REFERENCES captured_values(fingerprint),
    output_fingerprint BLOB NOT NULL REFERENCES captured_values(fingerprint),
    PRIMARY KEY (run_id, worker, ordinal)
) WITHOUT ROWID;
INSERT INTO runs DEFAULT VALUES;
"""


def record_worker(database, worker, settings):
    if settings.measure_memory:
        tracemalloc.start()
    context = (
        closing(sqlite3.connect(database, timeout=settings.busy_timeout))
        if database is not None
        else nullcontext()
    )
    write_seconds = 0.0
    slowest_batch = 0.0
    with context as connection:
        if connection is not None:
            connection.execute("PRAGMA foreign_keys = ON")
        for start in range(0, settings.calls, settings.batch_size):
            values = {}
            calls = []
            end = min(start + settings.batch_size, settings.calls)
            for ordinal in range(start, end):
                fingerprints = []
                for index in (ordinal, ordinal + 1):
                    value_id = (
                        worker * settings.calls + index
                    ) % settings.distinct_values
                    prefix = str(value_id).encode("ascii") + b":"
                    payload = prefix.ljust(settings.value_bytes, b"x")
                    fingerprint = hashlib.blake2b(payload, digest_size=16).digest()
                    values[fingerprint] = payload
                    fingerprints.append(fingerprint)
                parent = ordinal - ordinal % 10 if ordinal % 10 else None
                calls.append(
                    (1, worker, ordinal, parent, "example.call", *fingerprints)
                )
            if connection is not None:
                started = perf_counter()
                with connection:
                    connection.executemany(
                        "INSERT INTO captured_values VALUES (?, ?) "
                        "ON CONFLICT(fingerprint) DO NOTHING",
                        values.items(),
                    )
                    connection.executemany(
                        "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?)", calls
                    )
                elapsed = perf_counter() - started
                write_seconds += elapsed
                slowest_batch = max(slowest_batch, elapsed)
    peak = tracemalloc.get_traced_memory()[1] if settings.measure_memory else None
    if settings.measure_memory:
        tracemalloc.stop()
    return write_seconds, slowest_batch, peak


def measure(pool, database, settings):
    started = perf_counter()
    workers = range(settings.workers)
    results = list(
        pool.map(
            record_worker,
            [database] * settings.workers,
            workers,
            [settings] * settings.workers,
        )
    )
    return perf_counter() - started, results


def benchmark(directory, settings):
    database = Path(directory) / "trace.sqlite3"
    with closing(sqlite3.connect(database)) as reader:
        if reader.execute("PRAGMA journal_mode = WAL").fetchone() != ("wal",):
            raise RuntimeError("could not enable SQLite WAL mode")
        value_table_options = (
            " WITHOUT ROWID" if settings.value_layout == "without-rowid" else ""
        )
        reader.executescript(SCHEMA.format(value_table_options=value_table_options))
        with ProcessPoolExecutor(max_workers=settings.workers) as pool:
            # Spawn processes before timing either workload.
            list(pool.map(abs, range(-settings.workers, 0)))
            baseline, baseline_workers = measure(pool, None, settings)
            elapsed, writers = measure(pool, database, settings)

        total_calls = settings.calls * settings.workers
        call_count = reader.execute("SELECT count(*) FROM calls").fetchone()[0]
        value_count = reader.execute("SELECT count(*) FROM captured_values").fetchone()[
            0
        ]
        if (call_count, value_count) != (
            total_calls,
            min(total_calls + 1, settings.distinct_values),
        ):
            raise RuntimeError(
                f"unexpected stored counts: {call_count} calls, {value_count} values"
            )

        started = perf_counter()
        result = reader.execute(
            "SELECT v.payload FROM calls AS c JOIN captured_values AS v "
            "ON v.fingerprint = c.output_fingerprint "
            "WHERE c.run_id = ? AND c.worker = ? AND c.ordinal = ?",
            (1, settings.workers - 1, settings.calls - 1),
        ).fetchone()
        lookup_ms = (perf_counter() - started) * 1000
        expected_prefix = (
            str(total_calls % settings.distinct_values).encode("ascii") + b":"
        )
        if result != (expected_prefix.ljust(settings.value_bytes, b"x"),):
            raise RuntimeError("indexed call lookup returned the wrong value")

        sizes = {
            path.name: path.stat().st_size
            for path in database.parent.glob("trace.sqlite3*")
        }
        report = {
            "sqlite_version": sqlite3.sqlite_version,
            "settings": vars(settings),
            "calls": call_count,
            "distinct_values": value_count,
            "generate_without_storage_seconds": round(baseline, 3),
            "generate_and_store_seconds": round(elapsed, 3),
            "calls_per_second": round(total_calls / elapsed),
            "slowest_batch_write_ms": round(
                max(writer[1] for writer in writers) * 1000, 2
            ),
            "write_seconds_by_worker": [round(writer[0], 3) for writer in writers],
            "indexed_lookup_ms": round(lookup_ms, 3),
            "file_bytes": sizes,
        }
        if settings.measure_memory:
            report["peak_python_bytes_by_worker"] = [writer[2] for writer in writers]
            report["baseline_peak_python_bytes_by_worker"] = [
                worker[2] for worker in baseline_workers
            ]
        return report


def positive_int(value):
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=positive_int, default=1)
    parser.add_argument(
        "--calls", type=positive_int, default=100_000, help="calls per worker"
    )
    parser.add_argument("--batch-size", type=positive_int, default=1_000)
    parser.add_argument("--distinct-values", type=positive_int, default=10_000)
    parser.add_argument("--value-bytes", type=positive_int, default=256)
    parser.add_argument(
        "--value-layout", choices=("rowid", "without-rowid"), default="rowid"
    )
    parser.add_argument("--busy-timeout", type=positive_int, default=30, help="seconds")
    parser.add_argument(
        "--measure-memory",
        action="store_true",
        help="enable Python allocation tracking",
    )
    settings = parser.parse_args()
    required_bytes = (
        len(str(min(settings.calls * settings.workers, settings.distinct_values - 1)))
        + 1
    )
    if settings.value_bytes < required_bytes:
        parser.error(
            f"--value-bytes must be at least {required_bytes} for these settings"
        )
    with TemporaryDirectory(prefix="ldr-sqlite-") as directory:
        print(json.dumps(benchmark(directory, settings), indent=2))


if __name__ == "__main__":
    main()
