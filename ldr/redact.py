"""Secret detection. Best effort, applied before anything is hashed, stored or printed.

LDR's output goes to a model provider, and ``.ldr/`` is readable by the agent.
So a secret must never reach disk or output. A redacted value keeps no value
and no hash; it is replaced by a *labeled* placeholder so the reader still sees
what happened:

    <secret from env STRIPE_TEST_KEY>     the value equals/contains an env secret
    <secret: github-token, 40 chars>      a known shape
    <secret: by name, 24 chars>           the variable or key name looks secret

Detectors (no entropy guessing: it would hide hashes, ids and UUIDs):

1. Environment match: values of environment variables whose names look secret.
2. Known shapes: AWS, GitHub, Stripe, Slack, OpenAI/Anthropic, Google keys, JWTs,
   PEM private keys, ``user:password@`` in URLs, secret URL query parameters,
   Bearer headers.
3. Compound variable names only (``api_key``, ``auth_token``...). Never bare
   ``token`` or ``auth``: parser and lexer code uses ``token`` everywhere.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_SECRET_NAME = re.compile(
    r"(?:^|_)(?:password|passwd|secret|client_?secret|api_?key|api_?token|auth_?token|"
    r"access_?token|refresh_?token|authorization|cookie|session_?id|private_?key|"
    r"connection_?string|credentials?)(?:$|_)",
    re.IGNORECASE,
)


def is_secret_name(name: object) -> bool:
    if not isinstance(name, str):
        return False
    return bool(_SECRET_NAME.search(_CAMEL.sub("_", name)))


# (label, pattern, group to replace). Group 0 replaces the whole match.
_SHAPES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("pem-private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), 0),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}\b"), 0),
    # No leading \b on the distinctive prefixes: a secret glued to a word still leaks.
    ("github-token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"), 0),
    ("stripe-key", re.compile(r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"), 0),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("api-key", re.compile(r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), 0),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{8,})"), 1),
    ("url-credentials", re.compile(r"(?<=://)([^/\s:@]+:[^/\s@]+)(?=@)"), 1),
    ("url-query-secret", re.compile(
        r"(?i)(?<=[?&])(?:access_token|api_key|apikey|token|key|signature|sig)=([^&\s#]+)"), 1),
)

_ENV_NAME = re.compile(
    r"(?:^|_)(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?)$", re.IGNORECASE
)
_ENV_MIN_LENGTH = 8

_environment: dict[str, str] = {}  # secret value -> variable name


def load_environment(environ: Mapping[str, str] | None = None) -> int:
    """(Re)read secret-looking environment variables. Values stay in memory only."""
    _environment.clear()
    for name, value in (os.environ if environ is None else environ).items():
        if len(value) >= _ENV_MIN_LENGTH and (_ENV_NAME.search(name) or is_secret_name(name)):
            _environment[value] = name
    return len(_environment)


load_environment()


def placeholder(label: str, size: int | None = None) -> str:
    return f"<secret: {label}, {size} chars>" if size is not None else f"<secret: {label}>"


def by_name_placeholder(value: object) -> str:
    size = len(value) if isinstance(value, (str, bytes, bytearray)) else None
    return placeholder("by name", size)


# Cheap literal markers (lower case) per shape. A shape's regex only runs when one
# of them is present: scanning a 100 KB string with every regex cost ~27 ms.
_MARKERS: dict[str, tuple[str, ...]] = {
    "pem-private-key": ("private key",),
    "aws-access-key": ("akia", "asia", "agpa", "aida", "aroa", "anpa", "anva"),
    "github-token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    "stripe-key": ("_live_", "_test_"),
    "slack-token": ("xox",),
    "google-api-key": ("aiza",),
    "api-key": ("sk-",),
    "jwt": ("eyj",),
    "bearer-token": ("bearer",),
    "url-credentials": ("://",),
    "url-query-secret": ("=",),
}


def redact(text: str) -> tuple[str, int]:
    """Replace every detected secret in ``text``; return the new text and the count."""
    count = 0
    for secret, name in sorted(_environment.items(), key=lambda item: -len(item[0])):
        if secret in text:
            count += text.count(secret)
            text = text.replace(secret, f"<secret from env {name}>")
    # A credential given on the command line is a secret wherever it shows up
    # (for example inside the argv list a traced function receives).
    for secret in sorted(_argv_secrets, key=len, reverse=True):
        if len(secret) >= _ENV_MIN_LENGTH and secret in text:
            count += text.count(secret)
            text = text.replace(secret, "<secret from the command line>")
    lowered = text.lower()
    for label, pattern, group in _SHAPES:
        if not any(marker in lowered for marker in _MARKERS[label]):
            continue
        if group == 0:
            def swap(match, label=label):
                return placeholder(label, len(match.group(0)))
        else:
            def swap(match, label=label, group=group):
                whole, inner = match.group(0), match.group(group)
                start = match.start(group) - match.start(0)
                return whole[:start] + placeholder(label, len(inner)) + whole[start + len(inner):]
        text, found = pattern.subn(swap, text)
        count += found
    return text, count


def contains_secret(text: str) -> bool:
    return redact(text)[1] > 0


# On a command line a bare `--token` or `--auth` is a credential, unlike a
# variable called `token` in parser code, so flags get a slightly wider list.
_SECRET_FLAGS = {"token", "auth", "pass", "pwd", "bearer", "apikey"}


def _secret_flag(argument: str) -> tuple[str, str | None] | None:
    """For ``--name`` or ``--name=value`` return (name, value-or-None) if the name is secret."""
    if not argument.startswith("-"):
        return None
    name, equals, value = argument.lstrip("-").partition("=")
    normalized = name.lower().replace("-", "_")
    if normalized in _SECRET_FLAGS or is_secret_name(normalized):
        return name, (value if equals else None)
    return None


_argv_secrets: set[str] = set()


def register_argv_secrets(argv: list[str]) -> None:
    """Remember values given to secret flags on the raw command line.

    argparse can separate ``--token VALUE`` (the value lands among the
    positionals), so the pairing is learned from the raw order, once.
    """
    _argv_secrets.clear()
    hide_next = False
    for argument in argv:
        if hide_next and not argument.startswith("-"):
            _argv_secrets.add(argument)
            hide_next = False
            continue
        hide_next = False
        flag = _secret_flag(argument)
        if flag is not None:
            name, value = flag
            if value is None:
                hide_next = True
            elif value:
                _argv_secrets.add(value)


def redact_argv(arguments: list[str]) -> list[str]:
    """Redact credentials on a command line: ``--password=x``, ``--token x`` and
    any argument that matches the value detectors."""
    result: list[str] = []
    hide_next = False
    for argument in arguments:
        if argument in _argv_secrets:
            result.append(by_name_placeholder(argument))
            hide_next = False
            continue
        if hide_next and not argument.startswith("-"):
            result.append(by_name_placeholder(argument))
            hide_next = False
            continue
        hide_next = False
        flag = _secret_flag(argument)
        if flag is not None:
            name, value = flag
            dashes = argument[: len(argument) - len(argument.lstrip("-"))]
            if value is None:
                hide_next = "=" not in argument
                result.append(argument)
            else:
                result.append(f"{dashes}{name}={by_name_placeholder(value)}")
            continue
        result.append(redact(argument)[0])
    return result
