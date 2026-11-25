"""Compare the evidence of two successive runs of orders or toolz.

--a can be a checkout or a saved JSON dump. With no --b, print the dump.
With both sides, print every changed record and all five verdict counts.
"""

from __future__ import annotations

import argparse
import itertools
import json
import marshal
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import overhead
from dump_run import dump

COMMON = """
def c1_same(value):
    return value

def c1_changed(value):
    return VALUE

def c1_incomparable(api_key):
    return None

def c1_path():
    return PATH(11)
"""
VERSION = {"first": ("13", "c1_old"), "second": ("17", "c1_new")}
VERDICTS = ("same", "changed", "new", "missing", "not compared")


def source(version, *, pytest):
    value, path = VERSION[version]
    common = COMMON.replace("VALUE", value).replace("PATH", path)
    common += f"\ndef {path}(value):\n    return value\n"
    if pytest:
        stage = "removed" if version == "first" else "added"
        return common + f"""
from toolz import merge, take

def test_c1_toolz_operations():
    assert merge(dict(a=1), dict(b=2)) == dict(a=1, b=2)
    assert list(take(2, [1, 2, 3])) == [1, 2]

def test_c1_shared():
    assert c1_same(3) == 3
    assert c1_changed(7) == {value}
    assert c1_path() == 11
    assert c1_incomparable("test-fixture") is None

def test_c1_{stage}():
    assert c1_same(5) == 5
"""
    return common + '\nc1_same(3)\nc1_changed(7)\nc1_path()\nc1_incomparable("test-fixture")\n'


def run_and_dump(name, fixture, checkout):
    root, _, traced = fixture
    env = overhead.environment(Path(checkout).resolve(), root)
    env["PYTHONHASHSEED"] = "0"
    is_pytest = name == "toolz"
    path = root / ("toolz/tests/test_ldr_c1_records.py" if is_pytest else "app.py")
    if is_pytest:
        # The upstream suite samples randomness and hashes identity-based objects.
        # Use actual Toolz code with deterministic inputs for byte-level comparison.
        traced = traced[:4] + [path.relative_to(root).as_posix()] + traced[5:]
    original = path.read_bytes() if path.exists() else None
    prefix = b"" if is_pytest else original
    overhead.clean(root)
    try:
        for version in ("first", "second"):
            path.write_bytes(prefix + source(version, pytest=is_pytest).encode())
            overhead.timed(traced, root, env)
        return dump(root, include_records=True, include_verdicts=True)
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


def result_body(fixture, before, after):
    """Report environment independently from the result of comparing the calls."""
    before, after = (dict(side["records"]) for side in (before, after))
    for side in (before, after):
        side["status"] = dict(side["status"])
    old, new = (side["status"].pop("environment", None) for side in (before, after))
    if old != new:
        print(json.dumps({"fixture": fixture, "section": "environment", "before": old, "after": new}))
    return before, after


def same_marshaled_parts(before, after):
    # Marshal can set the reference flag differently for the same decoded string.
    return (before is not None and after is not None and before[:-1] == after[:-1]
            and marshal.loads(bytes.fromhex(before[-1])) == marshal.loads(bytes.fromhex(after[-1])))


def compare(name, before, after):
    valid = True
    for key in ("before", "after"):
        side = before if key == "before" else after
        counts = Counter(row[2] for row in side["verdicts"])
        print(json.dumps({"fixture": name, "checkout": key, "verdicts": {v: counts[v] for v in VERDICTS}}))
        valid &= all(counts[v] for v in VERDICTS)
    for section in sorted(before.keys() | after.keys()):
        old, new = before.get(section), after.get(section)
        if old == new:
            continue
        if section == "last.json" and old is not None and new is not None:
            old, new = result_body(name, old, new)
        elif isinstance(old, dict) and isinstance(new, dict):
            old, new = old.get("records", old), new.get("records", new)
        if old == new:
            continue
        if isinstance(old, list) and isinstance(new, list):
            for index, (left, right) in enumerate(itertools.zip_longest(old, new)):
                if left != right:
                    equivalent = section == "call_records" and same_marshaled_parts(left, right)
                    valid &= equivalent
                    print(json.dumps({"fixture": name, "section": section, "row": index,
                                      "before": left, "after": right, "decoded_equal": equivalent}))
        else:
            valid = False
            print(json.dumps({"fixture": name, "section": section, "before": old, "after": new}))
    return valid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True, type=Path)
    parser.add_argument("--b", type=Path)
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
    parser.add_argument("--only", nargs="+", choices=("orders", "toolz"), default=("orders", "toolz"))
    args = parser.parse_args()
    saved = json.loads(args.a.read_text(encoding="utf-8-sig")) if args.a.is_file() else {}
    results = {}
    valid = True
    for name in args.only:
        fixture = overhead.build(Path(args.work), name)
        before = saved[name] if saved else run_and_dump(name, fixture, args.a)
        if args.b:
            after = run_and_dump(name, fixture, args.b)
            valid &= compare(name, before, after)
        else:
            results[name] = before
            valid &= all(any(row[2] == verdict for row in before["verdicts"]) for verdict in VERDICTS)
    if not args.b:
        print(json.dumps(results, separators=(",", ":")))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
