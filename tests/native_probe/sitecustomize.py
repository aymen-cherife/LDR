"""Test-only parity assertion inherited by existing tests and their child processes."""
import os
from pathlib import Path

from ldr import fingerprint as fp

original = fp._fingerprint_details
native = fp._native


def checked(value, budget, partial_ok, sketch=False):
    before = {name: getattr(fp.STATS, name) for name in fp.STATS.__slots__}
    try:
        fp._native = None
        expected = original(value, budget, partial_ok, sketch)
    finally:
        fp._native = native
        for name, state in before.items():
            setattr(fp.STATS, name, state)
    actual = original(value, budget, partial_ok, sketch)
    if actual != expected:
        # Recorder error handling must not hide a mismatch from the outer test.
        with Path(os.environ["LDR_NATIVE_TEST_FAILURES"]).open("a") as log:
            log.write("native/Python fingerprint mismatch\n")
        raise AssertionError((type(value), actual, expected))
    return actual


if native is None:
    raise RuntimeError("native module not built")
fp._fingerprint_details = checked
