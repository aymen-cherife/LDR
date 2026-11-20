"""Per-run environment record: facts that can change behaviour without a code change.

A dependency bump or a different interpreter makes the same code behave
differently, and without this record that reads as a behaviour change with no
edit behind it. The record holds the Python and platform versions, the
installed package versions and the *names* of environment variables.
Variable values are never read into it: it is written to disk and printed,
and values are where secrets live.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import sysconfig
from hashlib import blake2b

from .redact import redact

# Past this many entries a comparison keeps counting but stops listing.
MAX_LISTED_CHANGES = 200


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _egg_info(path: str) -> tuple[str, str] | None:
    """Name and version from the header of an ``.egg-info`` PKG-INFO file."""
    name = version = None
    try:
        source = os.path.join(path, "PKG-INFO") if os.path.isdir(path) else path
        with open(source, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    break
                key, _, value = line.partition(":")
                if key == "Name":
                    name = value.strip()
                elif key == "Version":
                    version = value.strip()
                if name and version:
                    return name, version
    except OSError:
        pass
    return None


def _packages() -> dict[str, str]:
    """Installed distributions, read without parsing METADATA files.

    A ``name-version.dist-info`` directory carries both in its name, so listing
    each sys.path entry is enough. Parsing every METADATA file through
    importlib.metadata cost about 4 ms per package, a fixed price on every run.
    Legacy ``.egg-info`` entries are read from their short PKG-INFO header.
    """
    found: dict[str, str] = {}
    for entry in sys.path:
        try:
            with os.scandir(entry or ".") as listing:
                for item in listing:
                    name = item.name
                    if name.endswith(".dist-info"):
                        distribution, _, version = name[: -len(".dist-info")].partition("-")
                        pair = (distribution, version) if distribution and version else None
                    elif name.endswith(".egg-info"):
                        pair = _egg_info(item.path)
                    else:
                        continue
                    if pair is not None:
                        # The first match on sys.path is the one an import would find.
                        found.setdefault(_normalize(pair[0]), pair[1])
        except OSError:  # missing, a zip archive or not a directory
            continue
    return dict(sorted(found.items()))


def _platform() -> dict[str, str]:
    """The operating system, without ``platform.uname()``.

    On Windows that call starts a WMI query the first time, which took about
    400 ms. These sources are free.
    """
    if hasattr(os, "uname"):
        release = os.uname().release
    elif hasattr(sys, "getwindowsversion"):
        version = sys.getwindowsversion()
        release = f"{version.major}.{version.minor}.{version.build}"
    else:
        release = ""
    return {"system": sys.platform, "release": release, "tag": sysconfig.get_platform()}


def capture_environment(environ=None) -> dict:
    """Read the record. Only variable names are taken from ``environ``."""
    names = sorted({redact(name)[0] for name in (os.environ if environ is None else environ)})
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": _platform(),
        "packages": _packages(),
        "variables": names,
    }


def digest(record: dict) -> str:
    """Equal records give equal digests, independent of key order."""
    text = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return blake2b(text.encode(), digest_size=16).hexdigest()


def summarize(record: dict) -> dict:
    """The short form kept in every run's status."""
    system = record["platform"]
    return {
        "digest": digest(record),
        "python": record["python"]["version"],
        "platform": " ".join(filter(None, (system["system"], system["release"], system["tag"]))),
        "packages": len(record["packages"]),
        "variables": len(record["variables"]),
    }


def compare(before: dict, after: dict) -> list[dict]:
    """Every difference between two records, as facts (never causes)."""
    changes: list[dict] = []
    for field, label in (("python", "python"), ("platform", "platform")):
        old, new = before.get(field, {}), after.get(field, {})
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) != new.get(key):
                changes.append(
                    {"kind": label, "name": key, "before": old.get(key), "after": new.get(key)}
                )
    old_packages, new_packages = before.get("packages", {}), after.get("packages", {})
    for name in sorted(old_packages.keys() | new_packages.keys()):
        if old_packages.get(name) != new_packages.get(name):
            changes.append(
                {
                    "kind": "package",
                    "name": name,
                    "before": old_packages.get(name),
                    "after": new_packages.get(name),
                }
            )
    old_names, new_names = set(before.get("variables", ())), set(after.get("variables", ()))
    for name in sorted(old_names ^ new_names):
        changes.append(
            {
                "kind": "variable",
                "name": name,
                "before": "set" if name in old_names else None,
                "after": "set" if name in new_names else None,
            }
        )
    return changes


def describe_change(change: dict) -> str:
    kind, name, before, after = (change[key] for key in ("kind", "name", "before", "after"))
    if kind == "variable":
        return f"variable name {name} {'added' if after else 'removed'}"
    label = f"{kind} {name}"
    if before is None:
        return f"{label} added ({after})"
    if after is None:
        return f"{label} removed (was {before})"
    return f"{label} {before} -> {after}"


def comparison(baseline: dict | None, current: dict, baseline_record: dict | None, record: dict):
    """Compare this run's environment with the baseline's, saying what is unknown.

    ``baseline`` and ``current`` are the short summaries kept in each run's
    status. ``baseline_record`` is the baseline's full record when it could
    be read back.
    """
    if not baseline:
        return {
            "state": "unknown",
            "reason": "the baseline run recorded no environment",
            "changes": [],
            "omitted": 0,
        }
    if baseline.get("digest") == current["digest"]:
        return {"state": "same", "reason": None, "changes": [], "omitted": 0}
    if baseline_record is None:
        return {
            "state": "changed",
            "reason": "the baseline's full record could not be read, so the differences are not listed",
            "changes": [],
            "omitted": 0,
        }
    found = compare(baseline_record, record)
    return {
        "state": "changed",
        "reason": None,
        "changes": found[:MAX_LISTED_CHANGES],
        "omitted": max(0, len(found) - MAX_LISTED_CHANGES),
    }


def render_comparison(result: dict, current: dict, *, max_lines: int = 12) -> str:
    state = result["state"]
    if state == "same":
        return (
            f"ENVIRONMENT: same as the baseline (Python {current['python']}, "
            f"{current['packages']} packages, {current['variables']} variable names).\n"
        )
    if state == "unknown":
        return f"ENVIRONMENT: not compared; {result['reason']}.\n"
    changes = result["changes"]
    total = len(changes) + result["omitted"]
    head = (
        f"ENVIRONMENT CHANGED since the baseline ({total} difference(s))"
        if changes
        else "ENVIRONMENT CHANGED since the baseline"
    )
    if result["reason"]:
        return f"{head}; {result['reason']}.\n"
    lines = [f"  {describe_change(change)}" for change in changes[:max_lines]]
    if total > len(lines):
        lines.append(f"  (+{total - len(lines)} more; ldr show --environment lists each run's record)")
    return head + ":\n" + "\n".join(lines) + "\n"
