"""Bounded local snapshots for selected frames."""

import json
from dataclasses import dataclass, field

from .redact import redact
from .value_capture import encode_local


_COMPLETE_VALUE_OVERHEAD = len(b'{"status":"complete","value":}')


def encode_bindings(bindings, limit):
    # Pairs keep redacted names from overwriting one another.
    data = bytearray(b"[")
    count = 0
    for name, value in bindings:
        if type(name) is not str:
            continue
        key = json.dumps(redact(name)[0]).encode()
        available = limit - len(data) - len(key) - 5
        if available < 0:
            break
        evidence = encode_local(value, name, max(0, available - _COMPLETE_VALUE_OVERHEAD))
        if len(evidence) > available:
            break
        if count:
            data.extend(b",")
        data.extend(b"[" + key + b"," + evidence + b"]")
        count += 1
    else:
        return bytes(data + b"]"), True
    return bytes(data + b"]"), False


def _snapshot(frame, line, event, limit):
    prefix = json.dumps({"line": line, "event": event}, separators=(",", ":"))
    header = prefix[:-1].encode() + b',"locals":'
    complete = b',"locals_complete":true}'
    partial = b',"locals_complete":false}'
    available = limit - len(header) - len(partial)
    if available < len(b"[]"):
        return None
    data, fully_listed = encode_bindings(frame.f_locals.items(), available)
    return header + data + (complete if fully_listed else partial)


@dataclass(slots=True)
class Timeline:
    line: int | None = None
    observed: int = 0
    steps: list[bytes] = field(default_factory=list)
    exhausted: bool = False

    def advance(self, frame, next_line, event, limit):
        size = 0
        if self.line is not None:
            self.observed += 1
            if not self.exhausted:
                step = _snapshot(frame, self.line, event, limit)
                if step is None:
                    self.exhausted = True
                else:
                    self.steps.append(step)
                    size = len(step)
        self.line = next_line
        return size
