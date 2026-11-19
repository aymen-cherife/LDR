"""Bounded local snapshots for selected frames."""

import json
from dataclasses import dataclass, field

from .redact import redact
from .value_capture import encode_local


_COMPLETE_VALUE_OVERHEAD = len(b'{"status":"complete","value":}')


def _snapshot(frame, line, event, limit):
    # Locals are pairs so redacted names cannot overwrite one another.
    prefix = json.dumps({"line": line, "event": event}, separators=(",", ":"))
    data = bytearray(prefix[:-1].encode() + b',"locals":[')
    complete = b'],"locals_complete":true}'
    partial = b'],"locals_complete":false}'
    if len(data) + len(partial) > limit:
        return None
    count = 0
    for name, value in frame.f_locals.items():
        if type(name) is not str:
            continue
        key = json.dumps(redact(name)[0]).encode()
        available = limit - len(data) - len(partial) - len(key) - 4
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
        return bytes(data + complete)
    return bytes(data + partial)


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
