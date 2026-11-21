"""Count monitoring callbacks that land in code LDR does not record.

    python evaluation/foreign_callbacks.py DIR -- <ldr arguments>

Runs `ldr` in this process (as the child would) with every Tracer callback wrapped by a
counter, then prints one JSON line: callbacks into project code, into other code, and
what the other-code callbacks answered (DISABLE or nothing). Measurement only; the
wrappers are not part of LDR.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from ldr import tracer  # noqa: E402

COUNTS: Counter = Counter()
NAMES = ["_on_start", "_on_return", "_on_yield", "_on_resume", "_on_unwind", "_on_raise", "_on_throw", "_on_line"]


def wrap(name):
    original = getattr(tracer.Recorder, name, None)
    if original is None:
        return

    def counted(self, code, *rest):
        inside = self._in_scope(code.co_filename)
        answer = original(self, code, *rest)
        kind = "project" if inside else "other"
        COUNTS[f"{name}.{kind}"] += 1
        if not inside and answer is sys.monitoring.DISABLE:
            COUNTS[f"{name}.other.disabled"] += 1
        return answer

    setattr(tracer.Recorder, name, counted)


def main():
    index = sys.argv.index("--")
    os.chdir(sys.argv[1])
    sys.path.insert(0, os.getcwd())  # as `python -m` does
    for name in NAMES:
        wrap(name)
    os.environ["LDR_RUN_CHILD"] = "1"
    from ldr.cli import main as ldr_main

    try:
        ldr_main(sys.argv[index + 1:])
    finally:
        total = sum(COUNTS[k] for k in COUNTS if not k.endswith(".disabled"))
        other = sum(v for k, v in COUNTS.items() if k.endswith(".other"))
        print(json.dumps({"total": total, "other": other, "detail": dict(sorted(COUNTS.items()))}),
              file=sys.stderr)


if __name__ == "__main__":
    main()
