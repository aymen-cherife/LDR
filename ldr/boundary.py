"""Capture at selected Python library boundaries."""

import json
import sqlite3
from threading import RLock

from .deep import encode_bindings
from .redact import redact
from .structure import type_label
from .value_capture import encode_local

_RETURNED = b',"outcome":"returned","result":'
_RAISED = b',"outcome":"raised","exception":'


class BoundaryStream:
    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self.lock = RLock()
        self.active = {}
        self.pending = []
        self.started = 0
        self.stored = 0
        self.write_failed = False
        self.failure = None

    def start(self, fid, module, function, caller, bindings):
        with self.lock:
            if self.write_failed:
                return
            self.started += 1
            header = json.dumps(
                {"module": redact(module)[0], "function": redact(function)[0], "caller": caller},
                separators=(",", ":"),
            ).encode()[:-1] + b',"arguments":'
            entry = header + encode_bindings(bindings) + b',"arguments_complete":true'
            self.active[fid] = (self.started, entry)

    def close(self, fid, value, *, raised=False):
        if fid not in self.active:
            return
        with self.lock:
            saved = self.active.pop(fid, None)
            if saved is None:
                return
            ordinal, entry = saved
            if self.write_failed:
                return
            outcome = _RAISED if raised else _RETURNED
            if raised:
                value = type_label(type(value))
            result = encode_local(value, "")
            self.pending.append((ordinal, entry + outcome + result + b"}"))
            if len(self.pending) >= self.store.batch_size:
                self._flush()

    def _flush(self):
        if not self.pending:
            return
        try:
            self.stored += self.store.append_boundaries(self.worker, self.pending)
        except (OSError, EOFError, sqlite3.Error) as exc:
            self.write_failed = True
            self.failure = exc
        self.pending.clear()

    def finish(self):
        with self.lock:
            self._flush()
            unfinished = len(self.active)
            self.active.clear()
            return {
                "started": self.started,
                "stored": self.stored,
                "unfinished": unfinished,
                "write_failed": self.write_failed,
            }
