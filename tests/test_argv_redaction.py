"""The command line can carry credentials too: it is stored in .ldr/ and printed."""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))

sys.path.insert(0, os.path.dirname(HERE))

from ldr.redact import redact_argv  # noqa: E402


def test_secret_flags_and_values_are_redacted():
    assert redact_argv(["--password=hunter22pass", "--token", "abc123def456", "--api-key=zzzz9999"]) == [
        "--password=<secret: by name, 12 chars>",
        "--token",
        "<secret: by name, 12 chars>",
        "--api-key=<secret: by name, 8 chars>",
    ]
    assert redact_argv(["--client-secret", "s3cr3tvalue", "--verbose", "--auth=basic-abc"]) == [
        "--client-secret",
        "<secret: by name, 11 chars>",
        "--verbose",
        "--auth=<secret: by name, 9 chars>",
    ]


def test_ordinary_arguments_are_untouched():
    arguments = ["--verbose", "--key-name", "primary", "data.csv", "--tokens", "5", "-k", "test_x"]
    assert redact_argv(arguments) == arguments


def test_a_flag_followed_by_another_flag_does_not_swallow_it():
    assert redact_argv(["--token", "--verbose", "x"]) == ["--token", "--verbose", "x"]


def test_values_matching_a_known_shape_are_redacted_without_a_flag():
    token = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    assert token not in " ".join(redact_argv(["deploy", token, f"--url=https://x.io?{'token'}=abcdef123456"]))


APP = (
    "import sys\n"
    "def main(argv):\n"
    "    open('seen.txt', 'w').write(' '.join(argv))\n"
    "main(sys.argv[1:])\n"
)


def stored_text(tmp_path, result):
    everything = result.stdout + result.stderr
    for path in (tmp_path / ".ldr").rglob("*"):
        if path.is_file():
            everything += path.read_text(encoding="utf-8")
    return everything


def test_credentials_after_dashdash_are_hidden_but_the_program_still_gets_them(tmp_path):
    (tmp_path / "app.py").write_text(APP)

    result = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", "app.py", "--golden", "--",
         "--password=hunter22pass", "--token", "abc123def456", "keep-me"],
        cwd=tmp_path, env=ENV, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    assert (tmp_path / "seen.txt").read_text() == "--password=hunter22pass --token abc123def456 keep-me"
    everything = stored_text(tmp_path, result)
    assert "hunter22pass" not in everything and "abc123def456" not in everything
    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["rerun"]["arguments"] == [
        "--password=<secret: by name, 12 chars>", "--token", "<secret: by name, 12 chars>", "keep-me",
    ]


def test_credentials_are_hidden_even_when_argparse_separates_a_flag_from_its_value(tmp_path):
    """Without `--`, argparse moves `--token` away from its value; the pairing is
    learned from the raw command line, so the value is still found."""
    (tmp_path / "app.py").write_text(APP)

    result = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", "app.py", "--golden",
         "--password=hunter22pass", "--token", "abc123def456", "keep-me"],
        cwd=tmp_path, env=ENV, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    everything = stored_text(tmp_path, result)
    assert "hunter22pass" not in everything and "abc123def456" not in everything
    assert "keep-me" in everything


def test_inspect_refuses_to_rerun_with_redacted_arguments(tmp_path):
    (tmp_path / ".ldr").mkdir()
    (tmp_path / ".ldr" / "last.json").write_text(json.dumps({
        "frame_index": {"frames": [{"id": 1, "frame_id": "app.py:1:f#1"}]},
        "rerun": {"runner": "run", "target": "app.py", "scope": [], "project_root": ".",
                  "arguments": ["--token", "<secret: by name, 12 chars>"]},
    }))

    result = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "inspect", "--frame-id", "1"],
        cwd=tmp_path, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode == 2
    assert "redacted" in result.stderr and "inspect" in result.stderr
