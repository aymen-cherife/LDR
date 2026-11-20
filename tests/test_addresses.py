"""Call addresses: file:qualname#n along the call path, with no definition line."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

APP = """\
def inner(value):
    return value + 1


def outer():
    inner(1)
    inner(2)


outer()
outer()
"""


def launch(project, *args, expected_exit=0):
    result = subprocess.run(
        [sys.executable, "-m", "ldr", *args],
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    return result.stdout


def stored_addresses(project):
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        run_id = connection.execute(
            "SELECT id FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT frame_id FROM stream_calls WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()
    return [row[0] for row in rows]


def test_address_is_a_call_path_without_the_definition_line(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    assert stored_addresses(tmp_path) == [
        "app.py:outer#1",
        "app.py:outer#1/app.py:inner#1",
        "app.py:outer#1/app.py:inner#2",
        "app.py:outer#2",
        "app.py:outer#2/app.py:inner#1",
        "app.py:outer#2/app.py:inner#2",
    ]


def test_address_survives_an_edit_that_moves_the_functions(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    before = stored_addresses(tmp_path)
    (tmp_path / "app.py").write_text("# a comment\n# another\n\n" + APP, encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    assert stored_addresses(tmp_path) == before


def test_the_definition_line_stays_available_as_metadata(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())

    inner = [
        frame for frame in report["frame_index"]["frames"] if frame["function"] == "inner"
    ]
    assert len(inner) == 4
    assert all(frame["location"] == "app.py:1" for frame in inner)
    assert all(":1" not in frame["frame_id"].replace("#1", "") for frame in inner)


def test_function_selectors_carry_no_line_and_select_every_call(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    report = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    selectors = set()

    def collect(value):
        if isinstance(value, dict):
            if "inspect_selector" in value:
                selectors.add(value["inspect_selector"])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(report)
    assert "app.py:inner#*" in selectors

    launch(tmp_path, "inspect", "app.py", "--frame", "app.py:inner#*")
    inspected = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    frames = inspected["inspection"]["frames"]
    assert [frame["frame_id"] for frame in frames] == [
        "app.py:outer#1/app.py:inner#1",
        "app.py:outer#1/app.py:inner#2",
        "app.py:outer#2/app.py:inner#1",
        "app.py:outer#2/app.py:inner#2",
    ]


def test_one_call_can_be_selected_by_its_exact_address(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    launch(tmp_path, "inspect", "app.py", "--frame", "app.py:outer#2/app.py:inner#1")
    inspected = json.loads((tmp_path / ".ldr" / "last.json").read_bytes())
    assert [frame["frame_id"] for frame in inspected["inspection"]["frames"]] == [
        "app.py:outer#2/app.py:inner#1"
    ]


def test_under_pytest_the_root_is_the_test_node_id(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def apply_discount(total):\n    return total - 1\n\n\n"
        "def checkout(total):\n    return apply_discount(total)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_cart.py").write_text(
        "from cart import checkout\n\n\n"
        "def test_checkout():\n    assert checkout(10) == 9\n\n\n"
        "def test_checkout_again():\n    assert checkout(5) == 4\n",
        encoding="utf-8",
    )
    launch(tmp_path, "pytest", "test_cart.py")
    addresses = stored_addresses(tmp_path)
    assert "test_cart.py::test_checkout" in addresses
    assert "test_cart.py::test_checkout/cart.py:checkout#1" in addresses
    assert "test_cart.py::test_checkout/cart.py:checkout#1/cart.py:apply_discount#1" in addresses
    # Each test is its own root, so the ordinal restarts under it.
    assert "test_cart.py::test_checkout_again/cart.py:checkout#1" in addresses


def test_slashes_and_hashes_in_a_node_id_are_percent_encoded_in_its_segment(tmp_path):
    (tmp_path / "test_p.py").write_text(
        "import pytest\n\n\ndef helper(value):\n    return value\n\n\n"
        "@pytest.mark.parametrize('value', ['a/b', 'c#d', 'e%f'])\n"
        "def test_x(value):\n    assert helper(value) == value\n",
        encoding="utf-8",
    )
    launch(tmp_path, "pytest", "test_p.py")
    addresses = stored_addresses(tmp_path)
    roots = {address for address in addresses if "/" not in address.replace("test_p.py", "")}
    assert roots == {
        "test_p.py::test_x[a%2Fb]",
        "test_p.py::test_x[c%23d]",
        "test_p.py::test_x[e%25f]",
    }
    assert "test_p.py::test_x[a%2Fb]/test_p.py:helper#1" in addresses
