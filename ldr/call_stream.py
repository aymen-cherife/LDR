"""Record completed calls in bounded batches alongside the diagnostic tree."""

import json
import sqlite3
import threading
import time
from hashlib import blake2b
from time import perf_counter
from threading import RLock

from .boundary import BoundaryStream
from .deep import Timeline
from .fingerprint import PARTIAL_MARK, REDACTED, SKETCH_MARK, SKETCH_TYPES, STATS, is_digest
from . import previous as previous_runs
from .node import capture
from .sqlite_store import recording_failure
from .value_capture import capture_value


# A sending child flushes a partial batch at least this often, so a hard death
# loses at most about this much recording (or one batch).
FLUSH_INTERVAL = 0.1


def call_signature(parts, error):
    """One comparable value for a call: its error type and fingerprint rows."""
    pieces = [f"\x1f{role}\x1e{name}\x1e{token}" for role, name, token in parts]
    if error:
        pieces.insert(0, error)
    return blake2b("".join(pieces).encode("utf-8", "surrogatepass"), digest_size=8).digest()


# One encoder for every record: json.dumps with non-default options builds a new
# encoder on each call. The output is byte-for-byte the same.
_ENCODER = json.JSONEncoder(separators=(",", ":"), sort_keys=True)


def _encode(value):
    return _ENCODER.encode(value).encode()


class CallStream:
    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self.lock = RLock()
        self.active = {}
        self.next_ordinal = 0
        self.completed = 0
        self.uncomparable = 0
        # Values too large to walk in full: sketched instead (see fingerprint.configure).
        self.sketched_total = 0
        self.sketched_functions = {}
        self.sketched_examples = []
        self.redacted = 0
        # Where each recorded function is defined: {function: (file, line)}.
        self.locations = {}
        self.receivers_shared = 0
        self.pending = []
        self.pending_fp = []
        # The size of every value walked, per call (see fingerprint.STATS): (address, phase,
        # role, name, type, nodes), sent with the fingerprints.
        self.sizes = []
        self.counts = {}
        self.fingerprinted = 0
        self.stored = 0
        self.write_failed = False
        self.finished = False
        self.cost = {}
        self.shapes = {}
        self.incomplete = None
        self.last_address = None
        self.deep = {}
        self.deep_started = 0
        self.deep_observed = 0
        self.boundaries = BoundaryStream(store, worker) if store.capture_boundaries else None
        self.previous = None  # the previous run's signatures by address (previous.Previous)
        self.previous_error = None
        self.previous_load_s = None
        self.preview_of = None
        self.flusher_ident = None
        if getattr(store, "remote", False):
            flusher = threading.Thread(target=self._flush_periodically, name="ldr-flush", daemon=True)
            flusher.start()
            self.flusher_ident = flusher.ident
        # Tests whose every call keeps detail (a rerun for detail), by address.
        self.full_tests = tuple(getattr(store, "full_tests", ()))
        if store.reference_run:  # a script, or a rerun: its previous run is known at the start
            prefix = getattr(store, "reference_prefix", None)
            self._load_previous(store.reference_run, [prefix] if prefix else getattr(store, "reference_roots", None))

    def _load_previous(self, run_id=None, roots=None, node_ids=None, replicated=False) -> None:
        """Fetch the writer's fixed reference once and look signatures up locally."""
        started = perf_counter()
        try:
            snapshot = self.store.comparison_snapshot(run_id, roots, node_ids, replicated)
            if snapshot is not None:
                self.previous = previous_runs.Previous(*snapshot)
        except (OSError, EOFError, sqlite3.Error) as exc:
            self.previous_error = type(exc).__name__
        self.previous_load_s = round(perf_counter() - started, 3)

    def load_previous_for_tests(self, node_ids, *, replicated=False) -> None:
        if self.previous is None and not self.store.reference_run:
            self._load_previous(node_ids=node_ids, replicated=replicated)

    @property
    def keeping_details(self):
        return not (self.write_failed or (self.boundaries and self.boundaries.write_failed))

    def add_sizes(self, frame_id, rows):
        """The sizes of the values one call walked. The flusher thread sends and clears the list."""
        with self.lock:
            self.sizes.extend((frame_id, *row) for row in rows)

    def add_cost(self, function, nodes):
        """Charge fingerprint work (nodes walked) to a function."""
        self.cost[function] = self.cost.get(function, 0) + nodes

    def note_call(self, function, deep=False, address=None):
        """Count a started call and say why it keeps detail from entry, if it does.

        The first call of a function, a selected (deep) function and the first N
        calls keep detail. Past the ceiling the run stops: ``incomplete`` is set.
        """
        if not self.keeping_details:
            return None
        count = self.counts[function] = self.counts.get(function, 0) + 1
        if self.full_tests and address is not None and any(
            test == "*" or address == test or address.startswith(test + "/") for test in self.full_tests
        ):
            return "full"
        if count == 1:
            return "first call"
        if deep:
            return "deep"
        if count <= self.store.first_calls:
            return "first N"
        return None

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
            if detail:
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
        metadata, encoded = capture_value(value, fingerprint)
        if metadata["status"] != REDACTED:
            if is_digest(fingerprint):
                metadata["fingerprint"] = fingerprint
            metadata["preview"] = capture(value, name) if preview is None else preview
        if encoded is not None:
            data.append((phase, name, encoded))
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

    def record_line(self, fid, line):
        entry = self.active.get(fid)
        if entry is not None and entry[-1] is not None and self.keeping_details:
            entry[-1].setdefault(line, None)

    def record_deep(self, frame, next_line, event):
        with self.lock:
            timeline = self.deep.get(id(frame))
            if timeline is None:
                return
            if self.keeping_details:
                timeline.advance(frame, next_line, event)

    def resume_deep(self, frame):
        with self.lock:
            timeline = self.deep.get(id(frame))
            if timeline is not None:
                timeline.line = frame.f_lineno

    def close(
        self,
        frame,
        exit_inputs,
        output,
        error,
        output_preview,
        timing,
        return_value=None,
        shape=None,
        input_changes=None,
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
                is_digest(token) and not token.endswith((PARTIAL_MARK, SKETCH_MARK))
                for token in tokens
            )
            self.uncomparable += not comparable
            for name, token in (*inputs.items(), *([("return", output)] if error is None else [])):
                if token and token.endswith(SKETCH_MARK):
                    self.sketched_total += 1
                    self.sketched_functions[function] = self.sketched_functions.get(function, 0) + 1
                    if len(self.sketched_examples) < 20:
                        self.sketched_examples.append(
                            [entry[2], name, SKETCH_TYPES.get(token, "?"), token]
                        )
            self.redacted += sum(token == "redacted" for token in inputs.values()) + (output == "redacted")

            parts = [("in", name, token) for name, token in inputs.items()]
            parts.extend(
                ("out", name, token)
                for name, token in exit_inputs.items()
                if inputs.get(name) != token
            )
            if error is None:
                parts.append(("ret", "", output))
            error_type = error.split(":", 1)[0] if error else None
            signature = call_signature(parts, error_type)
            state = self._compare(frame_id, signature, comparable)
            self.pending_fp.append(
                (ordinal, frame_id, parent, function, error_type, parts,
                 None if comparable else "uncomparable", signature, state)
            )
            mutated = [name for name, token in inputs.items() if exit_inputs.get(name) != token]
            reasons = []
            if detail in ("first call", "deep"):
                reasons.append(detail)
            if shape is not None:
                seen = self.shapes.setdefault(function, set())
                if shape not in seen:
                    seen.add(shape)
                    if detail != "first call":
                        reasons.append("new shape")
            if state in ("changed", "new"):
                reasons.append("differs from reference")
            if error is not None:
                reasons.append("exception")
            late = False
            if not detail and reasons and self.keeping_details:
                # Detail that was not captured at entry is built now. An argument that
                # still has its entry fingerprint can be read from the frame; one that
                # changed is marked, and its detail needs a rerun.
                detail = late = True
                if self.preview_of is not None:
                    preview = self.preview_of(frame, mutated)
                if output_preview is None and error is None:
                    output_preview = capture(return_value)
                unchanged = {n: t for n, t in inputs.items() if n not in mutated}
                evidence = self._argument_values(frame, unchanged, "entry", preview or {})
                evidence[0].update(
                    {n: {"status": "mutated in call", "fingerprint": inputs[n]} for n in mutated}
                )
            entry_metadata, data = evidence or ({}, [])
            timeline = self.deep.pop(id(frame), None)
            if timeline is not None:
                self.deep_observed += timeline.observed
            if not (detail and self.keeping_details):
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
            if reasons:
                fields["kept"] = reasons
            if late:
                fields["detail_at"] = "return"
            if mutated:
                fields["mutated_in_call"] = mutated
            if input_changes:
                fields["input_changes"] = input_changes
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
            self.pending.append((ordinal, frame_id, parent, payload, data, timeline))
            if len(self.pending) >= self.store.batch_size or len(self.pending_fp) >= self.store.batch_size:
                self.flush()

    def _flush_periodically(self):
        """Send what is pending every FLUSH_INTERVAL. Runs on a thread LDR does not record."""
        while True:
            time.sleep(FLUSH_INTERVAL)
            with self.lock:
                if self.finished:
                    return
                if self.pending or self.pending_fp:
                    self.flush()
            if self.boundaries is not None:
                with self.boundaries.lock:
                    self.boundaries._flush()

    def _compare(self, frame_id, signature, comparable):
        """Compare one finished call with the previous run's call at the same address.

        Its verdict is sent with the call and also determines which calls keep detail.
        """
        previous = self.previous
        if previous is None or not previous.covers(frame_id):
            return None
        known = previous.signature(frame_id)
        if known is None:
            return "new"
        if known != signature:
            return "changed"
        return "same" if comparable else "not compared"

    def flush(self):
        if self.keeping_details and (self.pending or self.pending_fp):
            try:
                self.stored += self.store.append_stream(self.worker, self.pending, self.pending_fp, self.sizes)
                self.fingerprinted += len(self.pending_fp)
            except (OSError, EOFError, sqlite3.Error) as exc:
                self.write_failed = True
                self.incomplete = getattr(exc, "failure", None) or recording_failure(
                    self.store.path, exc, self.fingerprinted, self.last_address
                )
            else:
                if self.pending_fp:
                    self.last_address = self.pending_fp[-1][1]
            self.pending.clear()
            self.pending_fp.clear()
            self.sizes.clear()
        elif not self.keeping_details:
            self.pending.clear()
            self.pending_fp.clear()
            self.sizes.clear()

    def finish(self):
        with self.lock:
            self.finished = True
            self.flush()
            summary = {
                "started": self.next_ordinal,
                "stored": self.stored,
                "fingerprinted": self.fingerprinted,
                "incomplete": self.incomplete,
                "first_calls": self.store.first_calls,
                "last_calls": self.store.last_calls,
                "unfinished": len(self.active),
                "uncomparable": self.uncomparable,
                "redacted": self.redacted,
                "sketched": {
                    "total": self.sketched_total,
                    "functions": dict(sorted(self.sketched_functions.items(), key=lambda i: (-i[1], i[0]))[:20]),
                    "examples": self.sketched_examples,
                },
                "locations": {function: list(place) for function, place in self.locations.items()},
                "write_failed": self.write_failed,
                "calls": dict(sorted(self.counts.items(), key=lambda item: (-item[1], item[0]))[:50]),
                "fingerprint": {
                    "tmp_normalized": STATS.tmp_normalized,
                    "receivers_shared": self.receivers_shared,
                    "cost": dict(self.cost),
                },
            }
            if self.store.capture_deep:
                summary["deep"] = {
                    "matched": self.deep_started,
                    "unfinished": len(self.deep),
                    "observed": self.deep_observed
                    + sum(timeline.observed for timeline in self.deep.values()),
                }
            if self.previous is not None:
                summary["previous_loaded"] = len(self.previous)
                self.previous = None
            if self.previous_load_s is not None:
                summary["previous_load_s"] = self.previous_load_s
            if self.previous_error:
                summary["previous_error"] = self.previous_error
            if self.boundaries is not None:
                summary["boundaries"] = self.boundaries.finish()
                if self.boundaries.failure is not None and not summary["incomplete"]:
                    exc = self.boundaries.failure
                    summary["incomplete"] = getattr(exc, "failure", None) or recording_failure(
                        self.store.path, exc, self.fingerprinted, self.last_address
                    )
            self.active.clear()
            self.deep.clear()
        try:
            self.store.finish_stream(self.worker, summary)
        except (OSError, sqlite3.Error):
            summary["write_failed"] = True
        return summary
