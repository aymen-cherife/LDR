"""Speed step 4: remembered answers for small immutable values change no output."""

import math
import os
import random
import string
import tempfile

import pytest

from ldr import fingerprint as fp
from ldr import redact
from ldr.fingerprint import fingerprint_details

SHAPES = [
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "-----BEGIN PRIVATE KEY-----",
    "postgres://admin:hunter2pass@db.internal:5432/app",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
    "https://api.example.com/v1?token=abcdef1234567890&x=1",
]


def corpus(size=3243, seed=11):
    """A reproducible set of strings: known secret shapes, look-alikes and plain text.

    The 3,243-input corpus from the earlier redaction change was not kept, so
    this one has the same size and covers the same kinds of input.
    """
    rng = random.Random(seed)
    words = ["token", "auth", "key", "password", "api", "secret", "value", "naïve", "日本", "ok"]
    items = []
    while len(items) < size:
        kind = len(items) % 6
        if kind == 0:
            items.append(rng.choice(SHAPES))
        elif kind == 1:
            items.append(f"{rng.choice(words)} = {rng.choice(SHAPES)} ;")
        elif kind == 2:
            items.append("".join(rng.choice(string.printable) for _ in range(rng.randint(0, 40))))
        elif kind == 3:
            items.append(" ".join(rng.choice(words) for _ in range(rng.randint(1, 6))))
        elif kind == 4:
            items.append(rng.choice(SHAPES)[: rng.randint(3, 20)])  # truncated look-alikes
        else:
            items.append(os.path.join(tempfile.gettempdir(), f"tmp{rng.randint(0, 10**6)}", "x.csv"))
    return items


def values(seed=5):
    rng = random.Random(seed)
    scalars = [0.0, -0.0, math.nan, -math.nan, math.inf, -math.inf, 1e-310, True, False, 1, 0, -1,
               2**62, -(2**63), 2**64, None, "", "a", "é", "1", "True"]
    scalars += [rng.randint(-10**6, 10**6) for _ in range(200)]
    scalars += [rng.uniform(-1e6, 1e6) for _ in range(200)]
    scalars += corpus(400, seed=seed)
    tuples = [tuple(rng.choice(scalars) for _ in range(rng.randint(0, 8))) for _ in range(300)]
    return scalars + tuples + [(1, True), (True, 1), (0.0,), (-0.0,), (math.nan, 1)]


def answers(items):
    return [fingerprint_details(item) for item in items] + [fingerprint_details(item, partial_ok=True) for item in items]


@pytest.fixture
def uncached(monkeypatch):
    def compute(items, secrets=False):
        monkeypatch.setattr(fp, "CACHE", False)
        result = [fp._looks_secret(item) for item in items] if secrets else answers(items)
        monkeypatch.setattr(fp, "CACHE", True)
        return result
    return compute


def test_cached_fingerprints_match_the_uncached_path(uncached):
    items = values()
    expected = uncached(items)
    assert answers(items) == expected  # fills the cache
    assert answers(items) == expected  # answered from it


def test_cached_secret_checks_match_on_the_corpus(uncached):
    items = corpus()
    assert len(items) == 3243
    expected = uncached(items, secrets=True)
    assert [fp._looks_secret(item) for item in items] == expected
    assert [fp._looks_secret(item) for item in items] == expected
    assert uncached(items) == answers(items) == answers(items)


def test_true_one_and_signed_zeros_stay_apart():
    assert fingerprint_details(True) != fingerprint_details(1)
    assert fingerprint_details(0.0) != fingerprint_details(-0.0)
    assert fingerprint_details(math.nan) == fingerprint_details(math.nan)
    assert fingerprint_details((True,)) != fingerprint_details((1,))


def test_a_changed_redaction_environment_empties_the_caches():
    value = "release-candidate-7731"
    assert fingerprint_details(value)[0] != "redacted"
    try:
        redact.load_environment({"RELEASE_TOKEN": value})
        assert fingerprint_details(value)[0] == "redacted"
        assert fp._looks_secret(value)
    finally:
        redact.load_environment()
    assert fingerprint_details(value)[0] != "redacted"


def test_the_caches_are_bounded(monkeypatch):
    monkeypatch.setattr(fp, "CACHE_LIMIT", 100)
    for number in range(1000):
        fingerprint_details(number)
        fp._looks_secret(f"word{number}")
    assert len(fp._value_cache) <= 100 and len(fp._secret_cache) <= 100


def test_temp_paths_are_counted_every_time(monkeypatch):
    path = os.path.join(tempfile.gettempdir(), "tmpcache", "out.csv")
    before = fp.STATS.tmp_normalized
    for _ in range(3):
        fingerprint_details(path)
    assert fp.STATS.tmp_normalized == before + 3


def test_secret_names_are_cached_exactly():
    names = ["api_key", "token", "authToken", "password", "x", "client_secret", "auth"]
    first = [redact.is_secret_name(name) for name in names]
    assert first == [redact.is_secret_name(name) for name in names]
    assert first == [True, False, True, True, False, True, False]
