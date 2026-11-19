"""Defect 14: a secret must never reach disk or output.

Secret-shaped strings are assembled at runtime so this file contains no literal
token that a scanner could mistake for a real one.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldr import redact  # noqa: E402
from ldr.fingerprint import fingerprint  # noqa: E402
from ldr.node import capture  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

SHAPES = {
    "stripe-key": "sk_" + "live_" + "a1B2c3D4e5F6g7H8i9J0",
    "aws-access-key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github-token": "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "slack-token": "xox" + "b-" + "1234567890-abcdefghijkl",
    "api-key": "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0",
    "google-api-key": "AI" + "za" + "A" * 35,
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "pem-private-key": "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END " + "PRIVATE KEY-----",
    "url-credentials": "postgres://" + "admin:hunter2pass" + "@db.internal:5432/app",
    "bearer-token": "Bearer " + "abcdefgh12345678",
    "url-query-secret": "https://api.example.com/v1?" + "token=abcdef123456&page=2",
}
# The part of each sample that must never appear in output.
SECRET_PARTS = {
    "stripe-key": SHAPES["stripe-key"],
    "aws-access-key": SHAPES["aws-access-key"],
    "github-token": SHAPES["github-token"],
    "slack-token": SHAPES["slack-token"],
    "api-key": SHAPES["api-key"],
    "google-api-key": SHAPES["google-api-key"],
    "jwt": SHAPES["jwt"].split(".")[1],
    "pem-private-key": "MIIEvQIBADANBgkq",
    "url-credentials": "hunter2pass",
    "bearer-token": "abcdefgh12345678",
    "url-query-secret": "abcdef123456",
}


@pytest.mark.parametrize("label", sorted(SHAPES))
def test_known_shapes_are_redacted_with_a_label_everywhere(label):
    secret = SECRET_PARTS[label]
    value = SHAPES[label]
    nested = {"rows": [value], "pair": (value, 1)}

    for text in (capture(value), capture(nested), capture(Holder(value))):
        assert secret not in text, text
        assert f"<secret: {label}," in text, text
    assert fingerprint(value) == "redacted"
    assert fingerprint(nested) == "redacted"


class Holder:
    def __init__(self, payload):
        self.payload = payload


def test_a_secret_cut_by_the_preview_limit_still_leaves_no_fragment():
    value = "x" * 70 + SHAPES["github-token"]
    text = capture(value)
    assert SHAPES["github-token"][:20] not in text
    assert "<secret: github-token" in text


def test_compound_names_are_redacted_but_bare_token_is_not():
    for name in ("api_key", "auth_token", "authToken", "connection_string", "client_secret",
                 "refresh_token", "private_key", "credentials", "password"):
        assert capture("plain-value-1234", name) == "<secret: by name, 16 chars>", name
        assert fingerprint("plain-value-1234") != "redacted"  # the value alone is not a secret
    # parser and lexer code uses `token` and `auth` everywhere
    for name in ("token", "tokens", "auth", "next_token"):
        assert capture("NAME", name) == "'NAME'", name


def test_dict_and_object_fields_with_secret_names_are_labeled():
    assert capture({"api_key": "very-secret-value", "mode": "x"}) == (
        "{'api_key': '<secret: by name, 17 chars>', 'mode': 'x'}"
    )
    assert "password=<secret: by name, 8 chars>" in capture(Holder2("hunter22"))
    assert fingerprint({"password": "hunter22"}) == "redacted"
    assert fingerprint(Holder2("hunter22")) == "redacted"


class Holder2:
    def __init__(self, password):
        self.password = password


def test_environment_secrets_are_matched_by_value_and_named():
    redact.load_environment({
        "STRIPE_TEST_KEY": "env-secret-value-123",
        "PATH": "C:\\Windows\\path-that-is-long",
        "SHORT_TOKEN": "abc",
    })
    try:
        text = capture("prefix env-secret-value-123 suffix")
        assert "env-secret-value-123" not in text
        assert "<secret from env STRIPE_TEST_KEY>" in text
        assert fingerprint(["env-secret-value-123"]) == "redacted"
        assert "path-that-is-long" in capture("C:\\Windows\\path-that-is-long")  # not secret-named
        assert capture("abc") == "'abc'"  # too short to be a secret
    finally:
        redact.load_environment()


def test_exception_text_is_redacted_capped_and_never_raises():
    """Python's traceback and pytest call str(exc) anyway, so LDR keeps calling
    it, but guards it: no failure, bounded length, secrets removed."""
    from ldr.node import capture_error

    token = SHAPES["github-token"]
    text = capture_error(RuntimeError(f"auth failed for {token}"))
    assert token not in text and "<secret: github-token" in text and text.startswith("RuntimeError:")

    class BrokenStr(Exception):
        def __str__(self):
            raise ValueError("boom")

    assert capture_error(BrokenStr()) == "BrokenStr: <message unavailable>"

    huge = capture_error(RuntimeError(token + " " + "x" * 5_000_000 + " " + token))
    assert len(huge) < 800
    assert token not in huge and token[:12] not in huge

    redact.load_environment({"MY_API_TOKEN": "env-secret-value-555"})
    try:
        assert "env-secret-value-555" not in capture_error(KeyError("env-secret-value-555"))
    finally:
        redact.load_environment()


def test_a_crash_summary_never_carries_a_secret(tmp_path):
    token = SHAPES["stripe-key"]
    (tmp_path / "app.py").write_text(
        "def charge():\n"
        f"    raise RuntimeError('declined for {token}')\n"
        "charge()\n"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE))
    result = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", str(tmp_path / "app.py"), "--only", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1

    everything = result.stdout + result.stderr
    for path in (tmp_path / ".ldr").rglob("*"):
        if path.is_file():
            everything += path.read_bytes().decode("utf-8", errors="replace")
    assert token not in everything
    assert "RUN STATUS: CRASHED" in everything


def test_nothing_secret_reaches_the_output_files(tmp_path):
    secret_env = "env-secret-value-98765"
    (tmp_path / "app.py").write_text(
        "import os\n"
        "def call(payload, label):\n"
        "    return label\n"
        "samples = " + repr(list(SHAPES.values())) + "\n"
        "call(samples, 'ok')\n"
        "call({'k': os.environ['MY_SERVICE_TOKEN']}, 'env')\n"
        "call({'api_key': 'plain-value-1234'}, 'named')\n"
        "raise SystemExit(0)\n"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(HERE), MY_SERVICE_TOKEN=secret_env)
    result = subprocess.run(
        [sys.executable, "-m", "ldr.cli", "run", str(tmp_path / "app.py"), "--only", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    everything = result.stdout + result.stderr
    for path in (tmp_path / ".ldr").rglob("*"):
        if path.is_file():
            everything += path.read_bytes().decode("utf-8", errors="replace")
    for secret in [*SECRET_PARTS.values(), secret_env, "plain-value-1234"]:
        assert secret not in everything, secret

    payload = json.loads((tmp_path / ".ldr" / "last.json").read_text())
    assert payload["status"]["redacted_values"] >= 3
    assert "redacted, not compared" in (tmp_path / ".ldr" / "last.txt").read_text()
