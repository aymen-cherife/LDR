"""Local snapshots for selected frames."""

import json
from dataclasses import dataclass, field

from .redact import redact
from .value_capture import encode_local


_ENCODER = json.JSONEncoder(separators=(",", ":"))


def encode_bindings(bindings):
    # Pairs keep redacted names from overwriting one another.
    pairs = (
        b"[" + json.dumps(redact(name)[0]).encode() + b"," + encode_local(value, name) + b"]"
        for name, value in bindings if type(name) is str
    )
    return b"[" + b",".join(pairs) + b"]"


@dataclass(slots=True)
class Timeline:
    line: int | None = None
    observed: int = 0
    steps: list[bytes] = field(default_factory=list)

    def advance(self, frame, next_line, event):
        if self.line is not None:
            self.observed += 1
            header = _ENCODER.encode({"line": self.line, "event": event}).encode()
            data = encode_bindings(frame.f_locals.items())
            self.steps.append(header[:-1] + b',"locals":' + data + b',"locals_complete":true}')
        self.line = next_line
