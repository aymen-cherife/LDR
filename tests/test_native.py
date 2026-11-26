"""Byte parity for the optional signature prototype."""
import decimal
import functools
import hashlib
import random

import pytest

from ldr import fingerprint as fp

native = pytest.importorskip("ldr._native")


class Base:
    __slots__ = ("base", "unset")


class Slots(Base):
    __slots__ = ("__private", "__dict__")

    def __init__(self):
        self.base = 1
        self.__private = 2
        self.extra = 3


def result(value, native_reader, budget=fp.DEFAULT_BUDGET, partial=False):
    walk = fp._Walk(budget, partial)
    try:
        if native_reader:
            digest = native.encode(walk, value)
        else:
            out = hashlib.blake2b(digest_size=16)
            walk.encode(value, out)
            digest = out.hexdigest()
        return digest, walk.partial, walk.immutable
    except fp._Stop as stop:
        return stop.status


def test_native_matches_python():
    cycle = [1]
    cycle.append(cycle)
    obj = Slots()
    obj.extra = obj
    values = [None, True, False, 0, -(2**200), 2**300, -0.0, float("nan"),
              float("inf"), 1j, "\ud800", "/tmp/example", b"bytes", bytearray(b"mutable"),
              decimal.Decimal("1.20"), range(4), Ellipsis, NotImplemented,
              cycle, obj, {"a": 1, "b": 2}, {3, 1, "a"}, frozenset((1, 2)),
              functools.partial(pow, 2), {"api_key": "secret"}, ["ok"] * 50]
    rng = random.Random(0)
    for _ in range(100):
        values.append([rng.randrange(-100, 100), {"x": rng.random()}, tuple(range(rng.randrange(50)))])
    for value in values:
        for partial in (False, True):
            assert result(value, True, partial=partial) == result(value, False, partial=partial)


def test_native_ceiling_and_bulk_redaction():
    values = [list(range(100)), ["ok"] * 50, ["ok"] * 49 + ["api_key=not-for-storage"]]
    for value in values:
        for limit in (1, 20, 50, 100, 101):
            budget = fp.Budget(limit)
            assert result(value, True, budget) == result(value, False, budget)


def test_native_explicit_stack():
    value = 0
    # Keep every level alive so destruction is independent of the walk test.
    levels = [value]
    for _ in range(100_000):
        value = [value]
        levels.append(value)
    assert result(value, True) == result(value, False)


def test_layout_changes_and_descriptors():
    class Layout:
        pass

    class Descriptor:
        def __get__(self, instance, owner):
            raise AssertionError("descriptor executed")

    value = Layout()
    value.a = 1
    assert result(value, True) == result(value, False)
    Layout.x = Descriptor()
    value.b = 2
    assert result(value, True) == result(value, False)
