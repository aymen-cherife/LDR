"""Parity tests for the standalone native walk core, before runtime integration."""
import hashlib
import importlib.util
from pathlib import Path
import random
import sys
import pytest

from ldr import fingerprint as fp


def native():
    path = Path(__file__).parents[1] / 'ldr/_native/target' / f'py{sys.version_info.major}{sys.version_info.minor}' / 'release/lib_native.so'
    if not path.exists():
        pytest.skip('native module not built')
    spec = importlib.util.spec_from_file_location('_native', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_numeric_container_bytes_and_counts():
    module = native()
    rng = random.Random(0)
    values = [None, True, False, -(2**63), 2**63-1, -0.0, 0.0, float('inf'), float('-inf'), float('nan')]
    values += [[rng.randint(-100000,100000), (rng.random(), None, True)] for _ in range(100)]
    values += [{1: [2, 3], 4: (5, False)}]
    for value in values:
        class Bytes:
            def __init__(self): self.data = bytearray()
            def update(self, data): self.data.extend(data)
        output = Bytes()
        walk = fp._Walk(fp.DEFAULT_BUDGET)
        walk.encode(value, output)
        encoded, digest, visited = module.numeric_walk(value)
        assert encoded == bytes(output.data)
        assert digest == hashlib.blake2b(output.data, digest_size=16).hexdigest()
        assert visited == walk.visited


def test_cycles_and_100000_levels():
    module = native()
    cycle = []
    cycle.append(cycle)
    assert module.numeric_walk(cycle)[2] == 2
    deep = 1
    for _ in range(100000): deep = [deep]
    encoded, digest, visited = module.numeric_walk(deep)
    assert visited == 100001
    assert encoded == (b'l' + (1).to_bytes(8,'little')) * 100000 + b'i' + (1).to_bytes(8,'little',signed=True)


def test_unicode_buffer_preserves_utf8_and_surrogates():
    module = native()
    from test_value_cache import corpus
    values = ['', 'plain', 'é', '日本', '\U0001f600', '\ud800', '\udfff', 'a\ud800é\U0001f600\udfff']
    values.extend(corpus())
    values.extend(chr(code) for code in range(0xD800, 0xE000))
    for value in values:
        assert module.unicode_payload(value) == value.encode('utf8', 'surrogatepass')
