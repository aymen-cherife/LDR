"""Speed step 5: one shared JSON encoder writes the same bytes as json.dumps."""

import json
import math
import random

import pytest

from ldr import call_stream, deep, value_capture


def documents(seed=3):
    rng = random.Random(seed)

    def value(depth=0):
        kind = rng.randrange(8 if depth < 3 else 5)
        if kind == 0:
            return rng.randint(-10**12, 10**12)
        if kind == 1:
            return rng.uniform(-1e9, 1e9)
        if kind == 2:
            return "".join(rng.choice("ab\"\\é日\n\u2028") for _ in range(rng.randint(0, 12)))
        if kind == 3:
            return rng.choice([None, True, False])
        if kind == 4:
            return rng.choice([0.0, -0.0, 1e-310, 2**70])
        if kind == 5:
            return [value(depth + 1) for _ in range(rng.randint(0, 4))]
        if kind == 6:
            return {f"k{rng.randint(0, 9)}{index}": value(depth + 1) for index in range(rng.randint(0, 4))}
        return tuple(value(depth + 1) for _ in range(rng.randint(0, 3)))

    return [value() for _ in range(500)]


def test_records_are_encoded_byte_for_byte_as_before():
    for document in documents():
        expected = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        assert call_stream._encode(document) == expected
        assert value_capture._json(document) == json.dumps(
            document, separators=(",", ":"), allow_nan=False
        ).encode()
    assert deep._ENCODER.encode({"line": 3, "event": "line"}) == json.dumps(
        {"line": 3, "event": "line"}, separators=(",", ":")
    )


def test_value_encoding_still_refuses_nan_and_infinity():
    for bad in (math.nan, math.inf, [1, -math.inf]):
        with pytest.raises(ValueError):
            value_capture._json(bad)
