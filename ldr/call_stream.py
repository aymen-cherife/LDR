"""Record completed calls in bounded batches alongside the diagnostic tree."""

import json
import sqlite3
from hashlib import blake2b
from threading import RLock

from .boundary import BoundaryStream
from .deep import Timeline
from .fingerprint import PARTIAL_MARK, REDACTED, STATS, is_digest
from .node import capture
from .value_capture import capture_value


def call_signature(parts, error):
    """One comparable value for a call: its error type and fingerprint rows."""
    digest = blake2b(digest_size=8)
    digest.update((error or "").encode("utf-8", "surrogatepass"))
    for role, name, token in parts:
        digest.update(f"\x1f{role}\x1e{name}\x1e{token}".encode("utf-8", "surrogatepass"))
    return digest.digest()


def _encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


class CallStream:
    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self.lock = RLock()
        self.active = {}
        self.next_ordinal = 0
        self.completed = 0
        self.functions = {}
        self.pending = []
        self.pending_fp = []
        self.pending_bytes = 0
        self.counts = {}
        self.fingerprinted = 0
        self.stored = 0
        self.budget_exhausted = False
        self.write_failed = False
        self.finished = False
        self.value_bytes_in_flight = 0
        self.committed_value_bytes = 0
        self.cost = {}
        self.deep = {}
        self.deep_started = 0
        self.deep_observed = 0
        self.deep_bytes_in_flight = 0
        self.committed_deep_bytes = 0
        self.boundaries = BoundaryStream(store, worker) if store.max_boundary_bytes else None
        # The reference run's per-call signatures, loaded once, by address.
        self.reference = None
        self.reference_error = None
        self.reference_counts = {"compared": 0, "same": 0, "different": 0, "new": 0, "not_compared": 0}
        self.changed = {}
        self.preview_of = None
        if store.reference_run:
            try:
                self.reference = store.read_reference(store.reference_run, call_signature)
            except (OSError, sqlite3.Error) as exc:
                self.reference_error = type(exc).__name__

    @property
    def keeping_details(self):
        return not (self.budget_exhausted or self.write_failed)

    def add_cost(self, function, nodes):
        """Charge fingerprint work (nodes walked) to a function."""
        self.cost[function] = self.cost.get(function, 0) + nodes

    def note_call(self, function, deep=False):
        """Count a started call. True when it keeps detail: one of the first N of its function."""
        count = self.counts[function] = self.counts.get(function, 0) + 1
        return (deep or count <= self.store.first_calls) and self.keeping_details

    def detail_for(self, frame):
        entry = self.active.get(id(frame))
        return entry is not None and entry[7]

    def frame_address(self, frame):
        entry = self.active.get(id(frame))
        return entry[2] if entry is not None else None

    def start(self, frame, frame_id, function, inputs, preview, *, deep=False, detail=True):
        with self.lock:
            if self.finished:
                return
            self.next_ordinal += 1
            if deep:
                self.deep_started += 1
                self.deep[id(frame)] = Timeline()
            caller = frame.f_back
            parent = None
            while caller is not None and parent is None:
                parent = self.active.get(id(caller))
                caller = caller.f_back
            evidence = None
            if self.store.max_value_bytes and detail:
                evidence = self._argument_values(frame, inputs, "entry", preview or {})
            self.active[id(frame)] = (
                self.next_ordinal,
                parent[0] if parent else None,
                frame_id,
                function,
                inputs,
                preview,
                evidence,
                detail,
                {} if self.store.capture_lines and detail else None,
            )

    def _value(self, data, phase, name, value, fingerprint, preview=None):
        limit = (
            self.store.max_value_bytes
            - self.committed_value_bytes
            - self.value_bytes_in_flight
        )
        metadata, encoded = capture_value(value, fingerprint, max(0, limit))
        if metadata["status"] != REDACTED:
            if is_digest(fingerprint):
                metadata["fingerprint"] = fingerprint
            metadata["preview"] = capture(value, name) if preview is None else preview
        if encoded is not None:
            data.append((phase, name, encoded))
            self.value_bytes_in_flight += len(encoded)
        return metadata

    def _argument_values(self, frame, fingerprints, phase, previews):
        data = []
        locals_ = frame.f_locals
        metadata = {
            name: self._value(
                data, phase, name, locals_[name], token, previews.get(name)
            )
            for name, token in fingerprints.items()
            if name in locals_
        }
        return metadata, data

    def _release_values(self, data):
        self.value_bytes_in_flight -= sum(len(value) for _, _, value in data)

    def record_line(self, fid, line):
        entry = self.active.get(fid)
        if entry is not None and entry[-1] is not None and self.keeping_details:
            entry[-1].setdefault(line, None)

    def record_deep(self, frame, next_line, event):
        with self.lock:
            timeline = self.deep.get(id(frame))
            if timeline is None:
                return
            limit = 0
            if self.keeping_details:
                limit = self.store.max_deep_bytes - (
                    self.committed_deep_bytes + self.deep_bytes_in_flight
                )
            self.deep_bytes_in_flight += timeline.advance(frame, next_line, event, limit)

    def resume_deep(self, frame):
        with self.lock:
            timeline = self.deep.get(id(frame))
            if timeline is not None:
                timeline.line = frame.f_lineno

    def _release_deep(self, steps):
        self.deep_bytes_in_flight -= sum(map(len, steps))

    def close(
        self,
        frame,
        exit_inputs,
        output,
        error,
        output_preview,
        timing,
        return_value=None,
    ):
        with self.lock:
            entry = self.active.pop(id(frame), None)
            if entry is None:
                return
            ordinal, parent, frame_id, function, inputs, preview, evidence, detail, lines = (
                entry
            )
            self.completed += 1
            tokens = [*inputs.values(), *exit_inputs.values()]
            if error is None:
                tokens.append(output)
            comparable = all(
                is_digest(token) and not token.endswith(PARTIAL_MARK)
                for token in tokens
            )
            digest, count, unknown = self.functions.get(function, (None, 0, 0))
            if digest is None:
                digest = blake2b(digest_size=32)
            signature = _encode(
                (inputs, exit_inputs, output, error.split(":", 1)[0] if error else None)
            )
            digest.update(len(signature).to_bytes(8, "big"))
            digest.update(signature)
            self.functions[function] = (digest, count + 1, unknown + (not comparable))

            parts = [("in", name, token) for name, token in inputs.items()]
            parts.extend(
                ("out", name, token)
                for name, token in exit_inputs.items()
                if inputs.get(name) != token
            )
            if error is None:
                parts.append(("ret", "", output))
            error_type = error.split(":", 1)[0] if error else None
            state = self._compare(frame_id, function, parts, error_type, comparable)
            self.pending_fp.append((ordinal, frame_id, parent, function, error_type, parts, state))
            mutated = []
            late = False
            if state in ("different", "new") and not detail and self.keeping_details:
                # Detail that was not captured at entry is built now. An argument that
                # still has its entry fingerprint can be read from the frame; one that
                # changed is marked, and its detail needs a rerun.
                detail = late = True
                mutated = [name for name, token in inputs.items() if exit_inputs.get(name) != token]
                if self.preview_of is not None:
                    preview = self.preview_of(frame, mutated)
                if output_preview is None and error is None:
                    output_preview = capture(return_value)
                if self.store.max_value_bytes:
                    unchanged = {n: t for n, t in inputs.items() if n not in mutated}
                    evidence = self._argument_values(frame, unchanged, "entry", preview or {})
                    evidence[0].update(
                        {n: {"status": "mutated in call", "fingerprint": inputs[n]} for n in mutated}
                    )
            entry_metadata, data = evidence or ({}, [])
            timeline = self.deep.pop(id(frame), None)
            steps = timeline.steps if timeline is not None else []
            if timeline is not None:
                self.deep_observed += timeline.observed
            if not (detail and self.keeping_details):
                self._release_values(data)
                self._release_deep(steps)
                if len(self.pending_fp) >= self.store.batch_size:
                    self.flush()
                return
            fields = {
                "entry": inputs,
                "exit": exit_inputs,
                "return": output,
                "error": error,
                "input_preview": preview,
                "output_preview": output_preview,
                "elapsed_ms": timing["elapsed_ms"] if timing else None,
                "timing": timing,
            }
            if late:
                fields["detail_at"] = "return"
                if mutated:
                    fields["mutated_in_call"] = mutated
            if lines is not None:
                fields["executed_lines"] = list(lines)
                fields["completion_order"] = self.completed
            if evidence is not None:
                exit_metadata, exit_data = self._argument_values(
                    frame, exit_inputs, "exit", {}
                )
                data.extend(exit_data)
                fields["values"] = {"entry": entry_metadata, "exit": exit_metadata}
                if error is None:
                    fields["values"]["return"] = {
                        "result": self._value(
                            data,
                            "return",
                            "result",
                            return_value,
                            output,
                            output_preview,
                        ),
                    }
            payload = _encode(fields)
            size = len(payload) + len(frame_id.encode("utf-8", "surrogatepass"))
            if self.pending_bytes + size > self.store.max_trace_bytes:
                self.flush()
            if self.budget_exhausted or self.write_failed:
                self._release_values(data)
                self._release_deep(steps)
                return
            if size > self.store.max_trace_bytes:
                self.budget_exhausted = True
                self._release_values(data)
                self._release_deep(steps)
                return
            self.pending.append((ordinal, frame_id, parent, payload, data, timeline))
            self.pending_bytes += size
            if len(self.pending) >= self.store.batch_size or len(self.pending_fp) >= self.store.batch_size:
                self.flush()

    def _compare(self, frame_id, function, parts, error_type, comparable):
        """Compare one finished call with the reference run's call at the same address."""
        if self.reference is None:
            return None
        known = self.reference.pop(frame_id, None)
        counts = self.reference_counts
        if known is None:
            state = "new"
        else:
            counts["compared"] += 1
            if known[0] != call_signature(parts, error_type):
                state = "different"
            else:
                state = "same" if comparable else "not_compared"
        counts[state] += 1
        if state in ("different", "new"):
            self.changed[function] = self.changed.get(function, 0) + 1
        return state

    def flush(self):
        if self.pending or self.pending_fp:
            try:
                (
                    written,
                    self.committed_value_bytes,
                    self.committed_deep_bytes,
                ) = self.store.append_stream(self.worker, self.pending, self.pending_fp)
                self.stored += written
                self.fingerprinted += len(self.pending_fp)
                self.budget_exhausted |= written < len(self.pending)
            except (OSError, sqlite3.Error):
                self.write_failed = True
            for _, _, _, _, data, timeline in self.pending:
                self._release_values(data)
                if timeline is not None:
                    self._release_deep(timeline.steps)
            self.pending.clear()
            self.pending_fp.clear()
            self.pending_bytes = 0

    def finish(self):
        with self.lock:
            self.finished = True
            self.flush()
            summary = {
                "started": self.next_ordinal,
                "stored": self.stored,
                "fingerprinted": self.fingerprinted,
                "first_calls": self.store.first_calls,
                "last_calls": self.store.last_calls,
                "unfinished": len(self.active),
                "uncomparable": sum(
                    unknown for _, _, unknown in self.functions.values()
                ),
                "write_failed": self.write_failed,
                "fingerprint": {"tmp_normalized": STATS.tmp_normalized, "cost": dict(self.cost)},
                "functions": {
                    key: [digest.hexdigest(), count, unknown]
                    for key, (digest, count, unknown) in self.functions.items()
                },
            }
            if self.store.max_deep_bytes:
                summary["deep"] = {
                    "matched": self.deep_started,
                    "unfinished": len(self.deep),
                    "observed": self.deep_observed
                    + sum(timeline.observed for timeline in self.deep.values()),
                }
            if self.reference is not None or self.reference_error:
                missing = self.reference if self.reference is not None and self.worker == "main" else None
                changed = set(self.changed)
                if missing:
                    changed.update(function for _, function in missing.values())
                summary["reference"] = {
                    "run_id": self.store.reference_run,
                    "error": self.reference_error,
                    **self.reference_counts,
                    "missing": len(missing) if missing is not None else None,
                    "changed": sorted(changed),
                }
            if self.boundaries is not None:
                summary["boundaries"] = self.boundaries.finish()
            self.active.clear()
            self.deep.clear()
        try:
            self.store.finish_stream(self.worker, summary)
        except (OSError, sqlite3.Error):
            summary["write_failed"] = True
        return summary


def changed_functions(old, new):
    """Compare complete signatures by stable function identity, independent of worker."""

    def groups(summaries):
        result = {}
        for summary in summaries:
            if summary["write_failed"] or summary["unfinished"]:
                return None
            for key, (digest, count, unknown) in summary["functions"].items():
                result.setdefault(key, []).append((digest, count, unknown))
        return result

    before, after = groups(old), groups(new)
    if before is None or after is None:
        return []
    return [
        key
        for key in sorted(before.keys() | after.keys())
        if not any(item[2] for item in before.get(key, []) + after.get(key, []))
        and sorted(before.get(key, [])) != sorted(after.get(key, []))
    ]
