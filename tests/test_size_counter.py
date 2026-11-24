"""Batch 1, step 1: the size counter. Every value that is walked leaves a row in SQLite:
its type, its role (receiver, argument or return), the address of its call and its node count.
It changes nothing about what is recorded or compared."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from ldr import fingerprint
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

SCRIPT = """\
class Box:
    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)


def total(values):
    return sum(values)


box = Box(list(range(500)))
print(box.size(), total(list(range(50))))
"""


def ldr(project, *args):
    return processes.run(
        [sys.executable, "-m", "ldr", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )


def rows(project):
    with closing(sqlite3.connect(project / ".ldr" / "traces.sqlite3")) as connection:
        return connection.execute(
            "SELECT address, phase, role, name, type, nodes FROM value_sizes"
        ).fetchall()


def test_a_walk_leaves_its_type_and_node_count_in_the_stats():
    before = fingerprint.STATS.walks
    fingerprint.fingerprint(list(range(100)), budget=fingerprint.Budget())
    assert fingerprint.STATS.walks == before + 1
    assert fingerprint.STATS.walked_type is list
    assert fingerprint.STATS.walked >= 100


def test_every_walked_value_is_recorded_with_type_role_address_and_nodes(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    result = ldr(tmp_path, "run", "app.py")
    assert result.returncode == 0, result.stdout + result.stderr
    found = rows(tmp_path)
    by_key = {(address, role, name): (kind, nodes) for address, _, role, name, kind, nodes in found}
    # The receiver of a method is a Box, and walking it walks the 500 items it holds.
    kind, nodes = by_key[("app.py:Box.size#1", "receiver", "self")]
    assert kind == "Box" and nodes > 500
    # An argument and a return value.
    kind, nodes = by_key[("app.py:total#1", "argument", "values")]
    assert kind == "list" and nodes > 50
    kind, nodes = by_key[("app.py:total#1", "return", "")]
    assert kind == "int" and nodes >= 1
    # Entry and exit are told apart.
    phases = {phase for address, phase, role, *_ in found if address == "app.py:total#1"}
    assert phases == {"entry", "exit"}


def test_a_secret_named_argument_is_never_walked_so_it_leaves_no_row(tmp_path):
    (tmp_path / "app.py").write_text(
        "def login(password):\n    return len(password)\n\n\nlogin('hunter2-not-a-real-one')\n",
        encoding="utf-8",
    )
    assert ldr(tmp_path, "run", "app.py").returncode == 0
    assert not [row for row in rows(tmp_path) if row[3] == "password"]


LOOP = """\
def work(items):
    return len(items)


data = list(range(40))
for _ in range(6000):
    work(data)
    work(list(data))
"""


def test_no_walk_is_lost_between_the_recorder_and_the_flusher(tmp_path):
    """The rows add up to the fingerprint cost the run reports, even while a flusher thread sends."""
    import json

    (tmp_path / "app.py").write_text(LOOP, encoding="utf-8")
    assert ldr(tmp_path, "run", "app.py").returncode == 0
    with closing(sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3")) as connection:
        logged = connection.execute("SELECT SUM(nodes) FROM value_sizes").fetchone()[0]
        summary = json.loads(connection.execute("SELECT summary FROM stream_summary").fetchone()[0])
    assert logged == sum(summary["fingerprint"]["cost"].values())
