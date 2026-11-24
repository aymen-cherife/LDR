"""The parent writes; the child and its pytest workers only send.

The process that runs the target (and any pytest worker it starts) never writes
to SQLite. It sends its records over an authenticated local pipe to the ``ldr``
process that started it, which does all the writing. Records are ``marshal``
batches of plain tuples. When the target's process dies without warning, the
parent keeps everything it has received and marks the run with the last
address it saw; at most one batch can be missing.

``RemoteStore`` stands in for ``TraceStore`` in the sending processes.
The writer acknowledges committed batches or reports the first failure.
Before any read the child waits for the writer, then opens SQLite read-only.
"""

from __future__ import annotations

import json
import marshal
import os
import secrets
import sqlite3
import sys
import threading
from contextlib import closing
from multiprocessing.connection import Client, Listener
from pathlib import Path

from .sqlite_store import TraceStore, _call_rows, recording_failure, recording_failure_text

ENV = "LDR_SINK"


class _Run:
    def __init__(self, store, directory):
        self.store = store
        self.directory = directory
        self.finished = False
        self.last_address = None
        self.recording_failure = None
        self.status = None
        self.write_lock = threading.Lock()
        self.invocation = None  # what the program was started with, from its start message
        self.tests = 0  # tests that have started (pytest runs)
        self.lock = threading.Lock()
        self.counts = {}  # worker -> stored, fingerprinted, boundaries, write_failed

    def count(self, worker):
        return self.counts.setdefault(
            worker,
            {"stored": 0, "fingerprinted": 0, "boundaries": 0, "write_failed": False},
        )


class Sink:
    """The parent's end: accepts connections and writes what arrives."""

    def __init__(self):
        self.key = secrets.token_bytes(16)
        self.listener = Listener(authkey=self.key)
        self.token = f"{self.listener.address}|{self.key.hex()}"
        self.runs: dict[str, _Run] = {}
        self.main_run: str | None = None
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closing:
            try:
                connection = self.listener.accept()
            except Exception:
                if self.closing:
                    return
                continue
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _run_for(self, settings):
        with self.lock:
            run = self.runs.get(settings["run_id"])
            if run is None:
                run = self.runs[settings["run_id"]] = _Run(
                    TraceStore(**settings), str(Path(settings["path"]).parent)
                )
            return run

    def _serve(self, connection):
        run = None
        try:
            while True:
                try:
                    message = marshal.loads(connection.recv_bytes())
                except (EOFError, OSError):
                    return
                kind = message[0]
                if kind == "start":
                    _, directory, invocation, settings = message
                    try:
                        store = TraceStore.start(directory, json.loads(invocation), *settings)
                    except (OSError, sqlite3.Error) as exc:
                        failure = recording_failure(Path(directory) / "traces.sqlite3", exc, 0, None)
                        connection.send_bytes(marshal.dumps(("error", failure)))
                        continue
                    with self.lock:
                        run = self.runs[store.run_id] = _Run(store, directory)
                        run.invocation = json.loads(invocation)
                        if self.main_run is None:
                            self.main_run = store.run_id
                    connection.send_bytes(marshal.dumps(("ok", store.settings())))
                elif kind == "attach":
                    run = self._run_for(message[1])
                elif kind == "sync":
                    connection.send_bytes(marshal.dumps(("ok",)))
                else:
                    with run.write_lock:
                        self._apply(run, kind, message)
                        if kind in ("append", "boundaries"):
                            reply = ("error", run.recording_failure) if run.recording_failure else ("ok",)
                            connection.send_bytes(marshal.dumps(reply))
        finally:
            connection.close()

    def _apply(self, run, kind, message):
        store = run.store
        if run.recording_failure and kind in ("append", "boundaries"):
            return
        try:
            if kind == "append":
                _, worker, rows, fingerprints, sizes = message
                written = store.append_stream(worker, rows, fingerprints, sizes)
                with run.lock:
                    counts = run.count(worker)
                    counts["stored"] += written
                    counts["fingerprinted"] += len(fingerprints)
                    if fingerprints:
                        run.last_address = fingerprints[-1][1]
            elif kind == "boundaries":
                _, worker, rows = message
                written = store.append_boundaries(worker, rows)
                with run.lock:
                    run.count(worker)["boundaries"] += written
            elif kind == "finish_stream":
                _, worker, summary = message
                with run.lock:
                    counts = run.count(worker)
                    # The parent decided what was stored; its counts are the record.
                    summary["stored"] = counts["stored"]
                    summary["fingerprinted"] = counts["fingerprinted"]
                    summary["write_failed"] = summary.get("write_failed") or counts["write_failed"]
                    if "boundaries" in summary:
                        summary["boundaries"]["stored"] = counts["boundaries"]
                store.finish_stream(worker, summary)
            elif kind == "environment":
                store.save_environment(message[1])
            elif kind == "section":
                store.save_section(message[1], message[2])
            elif kind == "tests":
                store.save_tests(message[1], message[2])
                with run.lock:
                    run.tests += len(message[2])
            elif kind == "rng":
                store.save_rng(message[1], message[2])
            elif kind == "worker":
                _, worker, rows, summary = message
                store.save_worker_rows(worker, rows, summary)
            elif kind == "finish":
                # The indexed tables are built once the child has ended (see close).
                run.status = message[1]
                if run.recording_failure:
                    run.status["incomplete"] = run.recording_failure
                store.finish(run.status, build=False)
                run.finished = True
        except (OSError, sqlite3.Error) as exc:
            if kind in ("append", "boundaries", "finish_stream", "worker", "finish"):
                self._record_failure(run, kind, exc)
                if kind in ("append", "boundaries", "finish_stream"):
                    run.count(message[1])["write_failed"] = True

    @staticmethod
    def _record_failure(run, operation, exc):
        if run.recording_failure is None:
            with run.lock:
                calls = sum(count["fingerprinted"] for count in run.counts.values())
                failure = recording_failure(run.store.path, exc, calls, run.last_address)
                failure["operation"] = operation
                run.recording_failure = failure

    def close(self, exit_code: int, timeout: float = 30.0) -> str | None:
        """Finish writing after the child ended. Returns a message when it died."""
        for thread in list(self.threads):
            thread.join(timeout)
        self.closing = True
        try:
            self.listener.close()
        except OSError:
            pass
        for each in self.runs.values():
            try:
                each.store.build_lookup()
            except (OSError, sqlite3.Error) as exc:
                self._record_failure(each, "build indexes", exc)
        run = self.runs.get(self.main_run) if self.main_run else None
        if run is None:
            return None
        if run.recording_failure:
            return self._finish_incomplete(run, exit_code)
        if run.finished:
            return None
        batch = run.store.batch_size
        where = run.last_address or "(no call was received)"
        status = {
            "ok": False,
            "exit_code": exit_code,
            "died": True,
            "died_at": where,
            "missing_at_most": batch,
            "run_id": run.store.run_id,
        }
        message = (
            f"LDR: the run died (exit {exit_code}) at {where}. Everything received up to "
            f"then is stored in run {run.store.run_id}; at most one batch ({batch} calls) "
            "may be missing after that address."
        )
        try:
            run.store.finish(status, build=False)
        except (OSError, sqlite3.Error):
            pass
        _write_died_result(run.directory, status, message)
        return message

    @staticmethod
    def _finish_incomplete(run, exit_code):
        from .store import atomic_json, atomic_text

        failure = run.recording_failure
        message = recording_failure_text(failure)
        status = {
            **(run.status or {}),
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "run_id": run.store.run_id,
            "incomplete": failure,
        }
        try:
            run.store.finish(status, build=False)
        except (OSError, sqlite3.Error):
            pass
        # Keep the report if the child already published it. The supervisor has
        # the committed call count even when the child could not finish its report.
        name = "rerun" if status.get("rerun") else "last"
        json_path = Path(run.directory) / f"{name}.json"
        text_path = json_path.with_suffix(".txt")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        original = payload.get("status", {})
        if original.get("run_id") != run.store.run_id:
            payload = {"status": status, "findings": [], "failures": []}
            report = ""
        else:
            original.update(status)
            try:
                report = text_path.read_text(encoding="utf-8")
            except OSError:
                report = ""
        try:
            atomic_json(str(json_path), payload)
            if message not in report:
                atomic_text(str(text_path), message + "\n" + report)
        except OSError:
            pass
        return message


def _write_died_result(directory, status, message):
    from .store import atomic_json, atomic_text

    try:
        atomic_json(os.path.join(directory, "last.json"), {"status": status, "findings": [], "failures": []})
        atomic_text(os.path.join(directory, "last.txt"), message + "\n")
    except OSError:
        pass


class _Client:
    def __init__(self, token):
        address, key = token.rsplit("|", 1)
        self.connection = Client(address, authkey=bytes.fromhex(key))
        self.lock = threading.Lock()

    def send(self, message):
        data = marshal.dumps(message)
        with self.lock:
            self.connection.send_bytes(data)

    def request(self, message):
        data = marshal.dumps(message)
        with self.lock:
            self.connection.send_bytes(data)
            return marshal.loads(self.connection.recv_bytes())


class RecordingWriteError(OSError):
    def __init__(self, failure):
        super().__init__(failure["error"])
        self.failure = failure


class RemoteStore(TraceStore):
    """A ``TraceStore`` that sends its writes to the parent and reads read-only."""

    remote = True

    def __init__(self, *args, sink=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sink = sink
        self._client = None
        self._connect_lock = threading.Lock()

    @classmethod
    def start_remote(cls, sink, directory, invocation, *settings):
        client = _Client(sink)
        reply = client.request(("start", str(directory), json.dumps(invocation), list(settings)))
        if reply[0] == "error":
            raise RecordingWriteError(reply[1])
        store = cls(**reply[1], sink=sink)
        store._client = client
        return store

    def _send(self, message, *, acknowledge=False):
        if self._client is None:
            with self._connect_lock:
                if self._client is None:
                    client = _Client(self.sink)
                    settings = self.settings()
                    settings.pop("sink", None)
                    client.send(("attach", settings))
                    self._client = client
        if acknowledge:
            reply = self._client.request(message)
            if reply[0] == "error":
                raise RecordingWriteError(reply[1])
        else:
            self._client.send(message)

    def sync(self):
        if self._client is not None:
            self._client.request(("sync",))

    close = sync

    def _connect(self, *, allow_mode_change=False):
        return sqlite3.connect(
            Path(self.path).as_uri() + "?mode=ro", uri=True, timeout=self.busy_timeout
        )

    # --- writes: planned locally, sent to the parent

    def append_stream(self, worker, rows, fingerprints=(), sizes=()):
        packed = [
            (ordinal, frame_id, parent, payload, data,
             None if timeline is None else (timeline.observed, list(timeline.steps)))
            for ordinal, frame_id, parent, payload, data, timeline in rows
        ]
        self._send(("append", worker, packed, list(fingerprints), list(sizes)), acknowledge=True)
        return len(rows)

    def append_boundaries(self, worker, rows):
        self._send(("boundaries", worker, list(rows)), acknowledge=True)
        return len(rows)

    def finish_stream(self, worker, summary):
        self._send(("finish_stream", worker, summary))

    def save_environment(self, record):
        self._send(("environment", record))

    def save_section(self, name, text):
        self._send(("section", name, text))

    def save_tests(self, worker, rows):
        self._send(("tests", worker, list(rows)))

    def save_rng(self, name, payload):
        self._send(("rng", name, payload))

    def load_tests(self, run_id):
        self.sync()
        return super().load_tests(run_id)

    def load_rng(self, run_id, name):
        self.sync()
        return super().load_rng(run_id, name)

    def list_rng(self, run_id):
        self.sync()
        return super().list_rng(run_id)

    def load_section(self, run_id, name):
        self.sync()
        return super().load_section(run_id, name)

    def save_worker(self, worker, roots, summary):
        self._send(("worker", worker, list(_call_rows(roots)), summary))
        self.sync()

    def finish(self, status, *, build=True):
        self._send(("finish", status))
        self.sync()

    # --- reads: wait until the parent has written what was sent

    def read_stream(self, run_id):
        self.sync()
        return super().read_stream(run_id)

    def frame_values(self, run_id, frame_id):
        self.sync()
        return super().frame_values(run_id, frame_id)

    def value_bytes_used(self):
        self.sync()
        return super().value_bytes_used()

    def deep_totals(self):
        self.sync()
        return super().deep_totals()

    def boundary_bytes_used(self):
        self.sync()
        return super().boundary_bytes_used()

    def load_environment(self, run_id):
        self.sync()
        return super().load_environment(run_id)

    def load_worker(self, worker):
        self.sync()
        return super().load_worker(worker)


def store_from_settings(settings):
    """Open a run's store in a worker: remote when a sink is given."""
    if settings.get("sink"):
        return RemoteStore(**settings)
    return TraceStore(**settings)


def sink_from_environment() -> str | None:
    return os.environ.pop(ENV, None)
