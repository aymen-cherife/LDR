"""Value fingerprints: comparison must read the full value, not the preview."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldr.diff import diff_tree  # noqa: E402
from ldr.render import render_diff  # noqa: E402
from ldr.tracer import Recorder  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def record(fn):
    rec = Recorder(scope=[FIX])
    rec.start()
    try:
        fn()
    finally:
        rec.stop()
    return rec


def test_change_beyond_the_preview_is_reported():
    """Defect 1: items 1-10 vs the same list with item 8 changed.

    Both previews read ``[0, 1, 2, 3, 4, 5, ...]``, so comparing the preview
    reported no difference and the consumer got blamed."""
    import fixtures.values as v

    before = list(range(10))
    after = list(range(10))
    after[7] = 99
    old = record(lambda: v.consume(before))
    new = record(lambda: v.consume(after))
    assert old.roots[0].in_ == new.roots[0].in_  # previews really are identical

    changes = diff_tree(old.roots[0], new.roots[0])

    assert changes, "a change at item 8 of 10 must not read as 'unchanged'"
    text = render_diff(changes)
    assert "consume" in text
    assert "beyond the preview" in text


def test_old_schema_snapshot_compares_as_unknown_with_a_rerecord_hint(tmp_path):
    """Schema 6 snapshots hold previews only. They must never read as 'unchanged'."""
    import json
    import subprocess

    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    (tmp_path / "m.py").write_text(
        "def top(x):\n    return x + 1\n"
        "if __name__ == '__main__':\n    print(top(5))\n"
    )

    def run(*extra):
        return subprocess.run(
            [sys.executable, "-m", "ldr.cli", "run", str(tmp_path / "m.py"),
             "--only", str(tmp_path), *extra],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )

    assert run("--golden").returncode == 0
    snapshot = next((tmp_path / ".ldr" / "snapshots").glob("*.json"))
    payload = json.loads(snapshot.read_text())
    payload["schema_version"] = 6
    snapshot.write_text(json.dumps(payload))

    result = run("--diff", "--fail-on-change")

    assert "No behavioural change" not in result.stdout
    assert "unknown" in result.stdout.lower()
    assert "re-record" in result.stdout.lower()
    assert result.returncode == 3


def test_identical_long_values_are_equal():
    import fixtures.values as v

    old = record(lambda: v.consume(list(range(500))))
    new = record(lambda: v.consume(list(range(500))))
    assert diff_tree(old.roots[0], new.roots[0]) == []
