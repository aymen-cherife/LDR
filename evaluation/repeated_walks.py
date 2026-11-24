"""How many fingerprint walks read an object that an earlier walk had already read, unchanged.

    python evaluation/repeated_walks.py PROBE_PREFIX [--json]

PROBE_PREFIX names the rows written by evaluation/walk_probe/sitecustomize.py (one run of the
program under `ldr run`, with PYTHONPATH including evaluation/walk_probe and LDR_PROBE_OUT set):
for every walk, which object it read (same identity, the object kept alive so an id is never
reused), the digest, and the digest of the object's previous walk.

A walk is "repeated and unchanged" when its object was walked before and the digest is equal to
that of the last walk. Cost is counted in nodes (what the walk reads); the time of a walk is
roughly proportional to it. Digests that end in "~" (a sketch) or "+" (part of the object was
unreadable) or that are a status word do not prove the value unchanged: they are counted apart.

Then, for the repeated and unchanged walks, how many could be skipped exactly by a walk that
watches dictionaries (PEP 699 dict watchers) and nothing else: a skip is exact only if nothing
reachable from the object can change without a dict being written. A list, a set, a bytearray
or an object whose attributes live in slots can. The test uses a few samples of what each type's
nodes are made of (the "comp" rows), (the sample nearest in size to the value).
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import sys

UNREADABLE = ("+", "~")
STATUS_WORDS = {"unknown", "unsupported", "failed", "redacted", "partial"}
REASONS = {
    "holds a list": ("list", "fast_int", "fast_float", "fast_bool", "fast_str"),
    "holds a set": ("set",),
    "holds an object with slots and no __dict__": ("obj_noslotsdict",),
}
MUTABLE_WITHOUT_WATCHER = ("list", "set", "fast_int", "fast_float", "fast_bool", "fast_str", "obj_noslotsdict", "bytearray")


def load(prefix: str):
    walks, calls, comps = [], {}, collections.defaultdict(list)
    for path in sorted(glob.glob(prefix + ".*")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row[0] == "call":
                    calls[row[1]] = row[2]
                elif row[0] == "comp":
                    comps[row[2]].append((row[3], row[4]))
                else:
                    walks.append(row)
    return walks, calls, comps


def exact(token: str) -> bool:
    return bool(token) and token not in STATUS_WORDS and not token.endswith(UNREADABLE) and len(token) >= 16


_nearest_cache: dict[tuple, dict | None] = {}


def nearest_kinds(type_name: str, nodes: int, comps) -> dict | None:
    """What the nodes of this type were made of, from the sample nearest in size (None: no sample)."""
    key = (type_name, nodes)
    if key not in _nearest_cache:
        samples = comps.get(type_name)
        _nearest_cache[key] = None if not samples else min(
            samples, key=lambda sample: abs(math.log(max(sample[0], 1)) - math.log(max(nodes, 1)))
        )[1]
    return _nearest_cache[key]


def watchable(type_name: str, nodes: int, comps) -> str:
    """'yes', 'no' or 'unknown': could a dict watcher alone prove a value of this type and size unchanged?

    Judged by the sample of that type nearest in size (a ThemeStack that is still empty has
    nothing to hide; a full one holds a list)."""
    kinds = nearest_kinds(type_name, nodes, comps)
    if kinds is None:
        return "unknown"
    return "no" if any(kinds.get(kind, 0) for kind in MUTABLE_WITHOUT_WATCHER) else "yes"


def analyse(prefix: str) -> dict:
    walks, calls, comps = load(prefix)
    starts = {}
    for indices in calls.values():
        for index in indices:
            starts[index] = indices[0]
    last_walk: dict[int, int] = {}
    categories = collections.Counter()
    nodes = collections.Counter()
    times = collections.Counter()
    by_type = collections.defaultdict(lambda: collections.Counter())
    by_role = collections.defaultdict(lambda: collections.Counter())
    coverage = collections.Counter()
    reasons = collections.Counter()
    for row in walks:
        number, obj, type_name, size, role, phase, name, function, token, previous = row[:10]
        micros = row[10] if len(row) > 10 else 0
        before = last_walk.get(obj)
        last_walk[obj] = number
        if previous == "" or before is None:
            kind = "first walk of the object"
        elif token != previous:
            kind = "repeat, changed"
        elif not exact(token):
            kind = "repeat, equal but not exact (sketch, partial or status)"
        else:
            start = starts.get(number, number)
            kind = (
                "repeat, unchanged, same call (exit after entry)" if before >= start
                else "repeat, unchanged, earlier call"
            )
        categories[kind] += 1
        nodes[kind] += size
        times[kind] += micros
        if kind.startswith("repeat, unchanged"):
            by_type[type_name][kind] += size
            by_role[role][kind] += size
            verdict = watchable(type_name, size, comps)
            coverage[verdict] += size
            coverage["walks:" + verdict] += 1
            if verdict == "no":
                kinds = nearest_kinds(type_name, size, comps)
                for label, names in REASONS.items():
                    if any(kinds.get(name, 0) for name in names):
                        reasons[label] += size
    total_walks, total_nodes = len(walks), sum(nodes.values())
    return {
        "walks": total_walks, "nodes": total_nodes, "categories": dict(categories), "nodes_by_category": dict(nodes), "microseconds_by_category": dict(times),
        "by_type": {k: dict(v) for k, v in by_type.items()}, "by_role": {k: dict(v) for k, v in by_role.items()},
        "dict_watcher_coverage_nodes": {k: v for k, v in coverage.items() if not k.startswith("walks:")},
        "dict_watcher_coverage_walks": {k[6:]: v for k, v in coverage.items() if k.startswith("walks:")},
        "not_covered_because_nodes": dict(reasons),
    }


def report(result: dict) -> str:
    walks, total = result["walks"], result["nodes"]
    micros = result.get("microseconds_by_category", {})
    seconds = sum(micros.values()) / 1e6
    lines = [f"{walks:,} walks, {total:,} nodes" + (f", {seconds:,.1f} s inside the walks (probe time)" if seconds else ""), ""]
    lines.append(f"{'':62s}{'walks':>10s} {'share':>7s}{'nodes':>13s} {'share':>7s}{'seconds':>10s} {'share':>7s}")
    for kind in sorted(result["categories"], key=lambda k: -result["nodes_by_category"][k]):
        count, size = result["categories"][kind], result["nodes_by_category"][kind]
        spent = micros.get(kind, 0) / 1e6
        lines.append(
            f"{kind:62s}{count:10,d} {count / walks:7.1%}{size:13,d} {size / total:7.1%}"
            + (f"{spent:10.2f} {spent / seconds:7.1%}" if seconds else "")
        )
    unchanged = [k for k in result["categories"] if k.startswith("repeat, unchanged")]
    pair = sum(result["nodes_by_category"][k] for k in unchanged)
    count = sum(result["categories"][k] for k in unchanged)
    spent = sum(micros.get(k, 0) for k in unchanged) / 1e6
    lines += ["", f"repeated and unchanged, together: {count:,} walks ({count / walks:.1%}), {pair:,} nodes ({pair / total:.1%})"
              + (f", {spent:.2f} s ({spent / seconds:.1%} of the time inside walks)" if seconds else ""), ""]
    lines.append("repeated and unchanged, by type (nodes; the 10 largest)")
    ranked = sorted(result["by_type"].items(), key=lambda item: -sum(item[1].values()))[:10]
    for type_name, parts in ranked:
        lines.append(f"  {type_name:24s}{sum(parts.values()):13,d} {sum(parts.values()) / total:7.1%}")
    lines += ["", "repeated and unchanged, by role (nodes)"]
    for role, parts in sorted(result["by_role"].items()):
        lines.append(f"  {role:24s}{sum(parts.values()):13,d} {sum(parts.values()) / total:7.1%}")
    coverage = result["dict_watcher_coverage_nodes"]
    lines += ["", "of the repeated and unchanged nodes, how many a dict watcher alone could prove unchanged"]
    for verdict in ("yes", "no", "unknown"):
        if verdict in coverage:
            lines.append(f"  {verdict:24s}{coverage[verdict]:13,d} {coverage[verdict] / total:7.1%} of all nodes ({result['dict_watcher_coverage_walks'].get(verdict, 0):,} walks)")
    reasons = result.get("not_covered_because_nodes", {})
    if reasons:
        lines += ["", "the ones it could not (a value can have more than one reason; nodes)"]
        for label, size in sorted(reasons.items(), key=lambda item: -item[1]):
            lines.append(f"  {label:44s}{size:13,d} {size / total:7.1%} of all nodes")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prefix")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = analyse(args.prefix)
    print(json.dumps(result, indent=2) if args.json else report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
