"""Bounded capture at selected Python library boundaries."""

import json
import sqlite3
from threading import RLock

from .deep import encode_bindings
from .redact import redact
from .structure import type_label
from .value_capture import encode_local

_COMPLETE = b',"arguments_complete":true'
_PARTIAL = b',"arguments_complete":false'
_RETURNED = b',"outcome":"returned","result":'
_RAISED = b',"outcome":"raised","exception":'
_RESULT_RESERVE = max(len(_RETURNED), len(_RAISED)) + len(b'{"status":"unsupported"}}')
_VALUE_OVERHEAD = len(b'{"status":"complete","value":}')


class BoundaryStream:
    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self.lock = RLock()
        self.active = {}
        self.pending = []
        self.buffered_bytes = 0
        self.used_bytes = 0
        self.remaining_trace_bytes = store.max_trace_bytes
        self.started = 0
        self.stored = 0
        self.write_failed = False

    def _available(self):
        return min(
            self.store.max_boundary_bytes - self.used_bytes, self.remaining_trace_bytes
        ) - self.buffered_bytes

    def start(self, fid, module, function, caller, bindings):
        with self.lock:
            self.started += 1
            self.active[fid] = None
            if self._available() < _RESULT_RESERVE and self.pending:
                self._flush()
            if self.write_failed or self._available() < _RESULT_RESERVE:
                return
            header = json.dumps(
                {"module": redact(module)[0], "function": redact(function)[0], "caller": caller},
                separators=(",", ":"),
            ).encode()[:-1] + b',"arguments":'
            overhead = len(header) + len(_PARTIAL) + _RESULT_RESERVE
            available = self._available()
            if available < overhead + len(b"[]") and self.pending:
                self._flush()
                available = self._available()
            if self.write_failed or available < overhead + len(b"[]"):
                return
            arguments, complete = encode_bindings(bindings, available - overhead)
            entry = header + arguments + (_COMPLETE if complete else _PARTIAL)
            self.active[fid] = (self.started, entry)
            self.buffered_bytes += len(entry) + _RESULT_RESERVE

    def close(self, fid, value, *, raised=False):
        if fid not in self.active:
            return
        with self.lock:
            saved = self.active.pop(fid, None)
            if saved is None:
                return
            ordinal, entry = saved
            self.buffered_bytes -= len(entry) + _RESULT_RESERVE
            if self.write_failed:
                return
            outcome = _RAISED if raised else _RETURNED
            available = self._available()
            limit = available - len(entry) - len(outcome) - _VALUE_OVERHEAD - 1
            if raised:
                value = type_label(type(value))
            result = encode_local(value, "", max(0, limit))
            payload = entry + outcome + result + b"}"
            if len(payload) > available:
                return
            self.pending.append((ordinal, payload))
            self.buffered_bytes += len(payload)
            if len(self.pending) >= self.store.batch_size:
                self._flush()

    def _flush(self):
        if not self.pending:
            return
        try:
            (
                written, self.used_bytes, self.remaining_trace_bytes,
            ) = self.store.append_boundaries(self.worker, self.pending)
            self.stored += written
        except (OSError, EOFError, sqlite3.Error):
            self.write_failed = True
        self.buffered_bytes -= sum(len(payload) for _, payload in self.pending)
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
