"""Bounded, typed JSON for complete values already fingerprinted by LDR."""

import json
import math

from .fingerprint import DEFAULT_BUDGET, PARTIAL_MARK, REDACTED, is_digest
from .redact import contains_secret, is_secret_name

# Full value retention keeps its own node limit; the fingerprint walk has none.
VALUE_NODE_LIMIT = 10_000

# Shared, so each value does not build its own encoder. Same bytes as json.dumps.
_ENCODER = json.JSONEncoder(separators=(",", ":"), allow_nan=False)


class _Incomplete(Exception):
    pass


class _Redacted(_Incomplete):
    pass


class _Unsupported(_Incomplete):
    pass


def _one_of(kind, types):
    return any(kind is candidate for candidate in types)


def _json(value):
    return _ENCODER.encode(value).encode()


class _Encoder:
    def __init__(self, limit):
        self.limit = limit
        self.data = bytearray()
        self.ancestors = []
        self.nodes = 0

    def write(self, data):
        if len(self.data) + len(data) > self.limit:
            raise _Incomplete
        self.data.extend(data)

    def check_text(self, value):
        if len(value) > self.limit - len(self.data):
            raise _Incomplete
        if contains_secret(value):
            raise _Redacted

    def encode(self, value):
        self.nodes += 1
        if (
            self.nodes > VALUE_NODE_LIMIT
            or len(self.ancestors) > DEFAULT_BUDGET.max_depth
        ):
            raise _Incomplete
        kind = type(value)
        if value is None or kind is bool:
            self.write(_json(value))
        elif kind is str:
            self.check_text(value)
            self.write(_json(value))
        elif kind is int:
            if (value.bit_length() + 3) // 4 > self.limit - len(self.data):
                raise _Incomplete
            self.write(
                _json(
                    value
                    if value.bit_length() <= 53
                    else {"type": "int", "hex": format(value, "x")}
                )
            )
        elif kind is float:
            self.write(
                _json(
                    value
                    if math.isfinite(value)
                    else {"type": "float", "hex": value.hex()}
                )
            )
        elif kind is complex:
            self.write(
                _json(
                    {
                        "type": "complex",
                        "real": value.real.hex(),
                        "imag": value.imag.hex(),
                    }
                )
            )
        elif _one_of(kind, (bytes, bytearray)):
            if len(value) * 2 > self.limit - len(self.data):
                raise _Incomplete
            snapshot = bytes(value)
            self.check_text(snapshot.decode("latin-1"))
            self.write(_json({"type": kind.__name__, "hex": snapshot.hex()}))
        elif _one_of(kind, (list, tuple, dict, set, frozenset)):
            self.container(value, kind)
        else:
            raise _Unsupported

    def container(self, value, kind):
        marker = id(value)
        if marker in self.ancestors:
            self.write(
                _json(
                    {
                        "type": "cycle",
                        "distance": len(self.ancestors) - self.ancestors.index(marker),
                    }
                )
            )
            return
        self.ancestors.append(marker)
        if kind is not list:
            self.write(b'{"type":' + _json(kind.__name__) + b',"items":')
        self.write(b"[")
        if kind in (set, frozenset):
            start = len(self.data)
            parts = []
            for index, item in enumerate(value):
                if index:
                    self.write(b",")
                offset = len(self.data)
                self.encode(item)
                parts.append(bytes(self.data[offset:]))
            del self.data[start:]
            self.write(b",".join(sorted(parts)))
        else:
            for index, item in enumerate(value.items() if kind is dict else value):
                if index:
                    self.write(b",")
                if kind is dict:
                    key, element = item
                    if is_secret_name(key):
                        raise _Redacted
                    self.write(b"[")
                    self.encode(key)
                    self.write(b",")
                    self.encode(element)
                    self.write(b"]")
                else:
                    self.encode(item)
        self.write(b"]" if kind is list else b"]}")
        self.ancestors.pop()


def encode_local(value, name, limit):
    """Encode one local without collecting previews or fingerprints."""
    if is_secret_name(name):
        return b'{"status":"redacted"}'
    encoder = _Encoder(limit)
    try:
        encoder.encode(value)
    except _Redacted:
        return b'{"status":"redacted"}'
    except _Unsupported:
        return b'{"status":"unsupported"}'
    except (_Incomplete, RuntimeError, ValueError):
        return b'{"status":"partial"}'
    return b'{"status":"complete","value":' + bytes(encoder.data) + b"}"


def capture_value(value, fingerprint, limit):
    """Return shape, fallback status and optional complete payload. Never invokes app code."""
    if fingerprint == REDACTED:
        return {"status": REDACTED}, None
    kind = type(value)
    sized = _one_of(kind, (str, bytes, bytearray, list, tuple, dict, set, frozenset))
    shape = {
        "type": kind.__name__
        if sized or _one_of(kind, (type(None), bool, int, float, complex))
        else "object"
    }
    if sized:
        shape["length"] = len(value)
    if not is_digest(fingerprint):
        return {"status": fingerprint or "unknown", "shape": shape}, None
    fallback = {"status": "partial", "shape": shape}
    if not limit or fingerprint.endswith(PARTIAL_MARK):
        return fallback, None
    encoder = _Encoder(limit)
    try:
        encoder.encode(value)
    except (_Incomplete, RuntimeError, ValueError):
        return fallback, None
    return fallback, bytes(encoder.data)


def first_difference(before, after, path=""):
    """Find a concrete replacement or length change in two stored JSON values."""
    left, right = before, after
    if type(before) is dict and type(after) is dict:
        kind = before.get("type")
        if kind == after.get("type") and kind == "tuple":
            left, right = before["items"], after["items"]
        if kind == after.get("type") == "dict" and [
            _json(pair[0]) for pair in before["items"]
        ] == [_json(pair[0]) for pair in after["items"]]:
            for (key, old), (_, new) in zip(
                before["items"], after["items"], strict=True
            ):
                change = first_difference(old, new, f"{path}[{_json(key).decode()}]")
                if change is not None:
                    return change
            return None
    if type(left) is list and type(right) is list:
        for index, (old, new) in enumerate(zip(left, right)):
            change = first_difference(old, new, f"{path}[{index}]")
            if change is not None:
                return change
        if len(left) != len(right):
            return f"{path}.length", len(left), len(right)
    elif _json(before) != _json(after):
        return path, before, after
    return None
