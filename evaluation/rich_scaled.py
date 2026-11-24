"""A scaled copy of the rich benchmark: the same object types, sizes and mix of roles, 1/100 of the calls.

    python evaluation/rich_scaled.py generate OUT_DIR [--spec SPEC.json]
    python evaluation/rich_scaled.py extract PROBE_PREFIX RICH_DB RICH_ROOT [--divisor 100] [--spec OUT.json]

`generate` is deterministic: the same spec gives the same files. The fixture is `OUT_DIR/app.py`;
it imports this module (which lives outside the fixture directory, so LDR does not record it) to
build its objects before the first call. `extract` is a developers' tool: it rewrites
evaluation/rich_scaled_spec.json from a recorded rich run and the rows of the walk probe
(evaluation/walk_probe, read by evaluation/repeated_walks.py).

How the spec was made
---------------------
One recorded run of the rich benchmark (9 test files, 153,000 calls) with a probe on the
fingerprint walk. For every call it gives the function, its parameters and every value the walk
read: role (receiver, argument, return), type, nodes. Calls are sampled systematically (every
`divisor`-th call in recording order), so each function keeps its share of the calls and the
sampled calls keep their own values. For every type it keeps a few samples of what the nodes
were made of (objects, dicts, lists, strings, ints, long homogeneous lists that the walk reads
in bulk ...), and from the whole run how often a value was one that an earlier call had
already walked, and how often it had changed since. The fixture builds each value with the
composition of the sample nearest in size, and exactly the recorded number of nodes.

What the fixture does not copy: the work rich does between the calls, who calls whom (the driver
loop makes every call), exceptions, and growth during a call other than an object that is empty
at entry and full at exit (a receiver being constructed: 94% of the nodes that values gained during
a call in the rich run); the exit walk of any other value reads its entry size. A call whose receiver was shared with its caller (no
walk of it in the rich run) is made to a variant of the function without the receiver parameter.
"""

from __future__ import annotations

import ast
import collections
import json
import math
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "rich_scaled_spec.json"
FAST_PATH_MIN = 32  # LDR reads homogeneous runs of at least this many items in bulk
SAMPLES_PER_TYPE = 6

ROOTS = {"list": "list", "tuple": "tuple", "dict": "dict", "set": "set"}
PLAIN = ("none", "bool", "int", "float", "str", "bytes", "leaf")
FAST = ("fast_int", "fast_float", "fast_bool", "fast_str")
HOLDERS = ("obj", "obj_builtin", "dict", "list", "tuple", "set")
RECEIVERS = ("self", "cls")


# --------------------------------------------------------------------------------------
# Building a value with a given number of nodes
# --------------------------------------------------------------------------------------

_classes: dict[tuple, type] = {}


def _class(name: str, fields: int = -1) -> type:
    """A class named like the original (``fields`` >= 0: a named tuple of that many fields)."""
    import collections as _collections

    key = (name, fields)
    found = _classes.get(key)
    if found is None:
        name = "".join(c if c.isalnum() or c == "_" else "_" for c in name.rsplit(".", 1)[-1]) or "Value"
        if name[0].isdigit():
            name = "_" + name
        if fields >= 0:
            found = _collections.namedtuple(name, [f"f{i}" for i in range(fields)])
        else:
            found = type(name, (), {})
        found.__module__ = "fixture"
        _classes[key] = found
    return found


def nodes_of(value) -> int:
    """How many nodes the fingerprint walk reads in a value this module builds."""
    kind = type(value)
    if kind in (list, tuple, set, frozenset):
        return 1 + sum(nodes_of(item) for item in value)
    if kind is dict:
        return 1 + sum(nodes_of(key) + nodes_of(item) for key, item in value.items())
    if kind in (int, float, bool, str, bytes, type(None)) or callable(value) and not hasattr(value, "__dict__"):
        return 1
    if isinstance(value, tuple):  # a named tuple
        return 1 + sum(nodes_of(item) for item in value)
    return 1 + sum(nodes_of(item) for item in vars(value).values())


def _nearest(samples: list, nodes: int) -> tuple[int, dict]:
    best = min(samples, key=lambda sample: abs(math.log(max(sample[0], 1)) - math.log(max(nodes, 1))))
    return best[0], best[1]


def _scaled(composition: dict, sample_nodes: int, factor: float) -> dict:
    counts = {}
    for kind, count in composition.items():
        if kind in ("chars",) or kind.startswith("obj_") or not count:
            continue
        counts[kind] = max(1, int(count * factor)) if kind in HOLDERS else int(count * factor)
    for kind in FAST:
        if counts.get(kind, 0) < FAST_PATH_MIN:
            plain = "str" if kind == "fast_str" else {"fast_int": "int", "fast_float": "float", "fast_bool": "bool"}[kind]
            counts[plain] = counts.get(plain, 0) + counts.pop(kind, 0)
    return {kind: count for kind, count in counts.items() if count > 0}


def _leaf(kind: str, serial: int, chars: int):
    if kind == "none":
        return None
    if kind == "bool":
        return serial % 2 == 0
    if kind == "int":
        return 1000 + serial
    if kind == "float":
        return serial + 0.5
    if kind == "str":
        return f"s{serial}".ljust(chars, "x")
    if kind == "bytes":
        return b"b%d" % serial
    return len  # a built-in function: read by name


def _run(kind: str, size: int, chars: int) -> list:
    if kind == "fast_int":
        return [2000 + i for i in range(size)]
    if kind == "fast_float":
        return [i + 0.25 for i in range(size)]
    if kind == "fast_bool":
        return [i % 2 == 0 for i in range(size)]
    return [f"r{i}".ljust(chars, "y") for i in range(size)]


class _Part:
    __slots__ = ("kind", "children")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.children: list = []


def _structure(root_kind: str, counts: dict) -> _Part:
    """Holders in a tree (three children each), leaves dealt round the holders, fast runs in lists."""
    counts = dict(counts)
    counts[root_kind] = max(1, counts.get(root_kind, 0))
    holders = [_Part(root_kind)]
    counts[root_kind] -= 1
    for kind in HOLDERS:
        holders.extend(_Part(kind) for _ in range(counts.pop(kind, 0)))
    for index, part in enumerate(holders[1:], start=1):
        holders[(index - 1) // 3].children.append(("part", part))
    runs = [(kind, counts.pop(kind)) for kind in FAST if counts.get(kind)]
    lists = [part for part in holders if part.kind == "list"] or [holders[0]]
    for number, (kind, size) in enumerate(runs):
        lists[number % len(lists)].children.insert(0, ("run", kind, size))
    dealt = [part for part in holders if part.kind != "set"] or holders
    serial = cursor = 0
    pending = {kind: counts.get(kind, 0) for kind in PLAIN}
    while any(pending.values()):
        for kind in PLAIN:
            if pending[kind]:
                pending[kind] -= 1
                dealt[cursor % len(dealt)].children.append(("leaf", kind, serial))
                cursor += 1
                serial += 1
    return holders[0]


def _materialize(part: _Part, name: str, chars: int, bump: list):
    items, runs = [], []
    for entry in part.children:
        if entry[0] == "part":
            items.append(_materialize(entry[1], "Part", chars, bump))
        elif entry[0] == "leaf":
            items.append(_leaf(entry[1], entry[2], chars))
        else:
            runs.append(_run(entry[1], entry[2], chars))
    kind = part.kind
    if kind == "list":
        result = [value for run in runs for value in run] + items
        if not bump:
            for index, value in enumerate(result):
                if type(value) is int:
                    bump.append((result, index))
                    break
        return result
    if kind == "tuple":
        return tuple(items + [value for run in runs for value in run])
    if kind == "set":
        return set(range(len(items))) | {value for run in runs for value in run if type(value) is int}
    if kind == "dict":
        result = {f"k{index}": item for index, item in enumerate(items)}
        for index, run in enumerate(runs):
            result[f"run{index}"] = run
        if not bump:
            for key, value in result.items():
                if type(value) is int:
                    bump.append((result, key))
                    break
        return result
    if kind == "obj_builtin":
        values = items + runs
        return _class(name, len(values))(*values)
    instance = _class(name)()
    attributes = vars(instance)
    for index, item in enumerate(items):
        attributes[f"a{index}"] = item
    for index, run in enumerate(runs):
        attributes[f"run{index}"] = run
    if not bump:
        for key, value in attributes.items():
            if type(value) is int:
                bump.append((attributes, key))
                break
    return instance


def _items(kind: str, size: int, chars: int) -> list:
    if kind.startswith("fast") and size >= FAST_PATH_MIN:
        return _run(kind, size, chars)
    plain = {"fast_int": "int", "fast_float": "float", "fast_bool": "bool", "fast_str": "str"}.get(kind, kind)
    return [_leaf(plain, 5000 + i, chars) for i in range(size)]


def _pad(value, root_kind: str, delta: int, kind: str, chars: int) -> bool:
    """Add exactly ``delta`` nodes to the root. False when that is not possible for this root."""
    if delta == 0:
        return True
    if root_kind == "list":
        value.extend(_items(kind, delta, chars))
    elif root_kind == "set":
        value.update(range(10_000, 10_000 + delta))
    elif root_kind == "dict":
        if delta == 1:
            return False
        if delta % 2:
            value["padodd"] = [0]  # a key, a list and an int
            delta -= 3
        for index, item in enumerate(_items(kind if not kind.startswith("fast") else "int", delta // 2, chars)):
            value[f"pad{index}"] = item
    elif delta == 1:
        vars(value)["pad"] = 0
    else:
        vars(value)["pad"] = _items(kind, delta - 1, chars)  # the list is one node
    return True


def build(name: str, nodes: int, samples: list | None):
    """A value of type ``name`` with exactly ``nodes`` nodes. Returns (value, bump); ``bump`` is a
    (container, key) whose int is incremented to change the value, or None."""
    if nodes <= 1 or not samples:
        return _small(name, nodes), None
    sample_nodes, composition = _nearest(samples, nodes)
    dominant = max(("fast_int", "fast_str", "fast_float", "int", "str"), key=lambda kind: composition.get(kind, 0))
    chars = max(2, round(composition.get("chars", 0) / max(1, composition.get("str", 0) + composition.get("fast_str", 0))))
    root_kind = ROOTS.get(name) or ("obj_builtin" if composition.get("obj_builtin") and not composition.get("obj") else "obj")
    factor = nodes / sample_nodes
    for _ in range(60):
        counts = _scaled(composition, sample_nodes, factor)
        value, bump = None, []
        value = _materialize(_structure(root_kind, counts), name, chars, bump)
        delta = nodes - nodes_of(value)
        if delta < 0:
            factor *= max(0.5, (nodes + delta * 1.05) / nodes)
            continue
        if root_kind in ("tuple", "obj_builtin"):
            value = _pad_immutable(value, root_kind, name, delta) if delta else value
            break
        if _pad(value, root_kind, delta, dominant, chars):
            break
        factor *= 0.995
    else:
        value, bump = _generic(name, root_kind, nodes)
    if nodes_of(value) != nodes:
        raise ValueError(f"{name}: built {nodes_of(value)} nodes, wanted {nodes}")
    return value, (bump[0] if bump else None)


def _generic(name: str, root_kind: str, nodes: int):
    """A value of exactly ``nodes`` nodes, made of ints, when the sample's composition does not fit."""
    ints = list(range(9000, 9000 + nodes))
    if root_kind == "list":
        value = ints[: nodes - 1]
        return value, ((value, 0) if value else None)
    if root_kind == "tuple":
        return tuple(ints[: nodes - 1]), None
    if root_kind == "set":
        return set(ints[: nodes - 1]), None
    if root_kind == "dict":
        if nodes % 2 == 0:  # a dict is 1 + 2 per entry; one entry holds a one-item list
            value = {f"k{i}": i for i in range((nodes - 4) // 2)} if nodes >= 4 else {}
            if nodes >= 4:
                value["tail"] = [0]
            return value, None
        value = {f"k{i}": 1000 + i for i in range((nodes - 1) // 2)}
        return value, (((value, "k0")) if value else None)
    if root_kind == "obj_builtin":
        values = tuple(ints[: nodes - 1])
        return _class(name, len(values))(*values), None
    instance = _class(name)()
    attributes = vars(instance)
    for index in range(nodes - 1):
        attributes[f"a{index}"] = 1000 + index
    return instance, ((attributes, "a0") if nodes > 1 else None)


def _pad_immutable(value, root_kind: str, name: str, delta: int):
    if root_kind == "tuple":
        return value + tuple(range(7000, 7000 + delta))
    values = tuple(value) + tuple(range(7000, 7000 + delta))
    return _class(name, len(values))(*values)


def _small(name: str, nodes: int):
    scalars = {"str": "x", "int": 1, "float": 1.5, "bool": True, "NoneType": None, "bytes": b"x"}
    if name in scalars:
        return scalars[name]
    if name in ROOTS:
        return {"list": [], "tuple": (), "dict": {}, "set": set()}[name]
    return _class(name)()


# --------------------------------------------------------------------------------------
# The fixture
# --------------------------------------------------------------------------------------

def load(path: Path = SPEC_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare(spec: dict):
    """Every call of the fixture, ready to be made: (function index, arguments, return value, bumps, growth).

    Values are built here, before the first call, so building costs the traced run nothing more
    than the plain one. A value marked "reuse" is the object an earlier call used, if one of the
    same type and size exists; "chg" changes it first, so its digest differs from the last walk's.
    A value that was empty at entry and full at exit (a receiver being constructed) is made empty
    here; the function fills it from ``growth``, a list of (method, argument) pairs."""
    types_ = spec["types"]
    created: dict[tuple, list] = {}
    steps = []
    for call in spec["calls"]:
        function = spec["functions"][call["f"]]
        arguments = [1] * len(function["params"])
        bumps, growth = [], []

        def value_for(type_index, nodes, reuse, chg, grown):
            name = spec["type_names"][type_index]
            if grown and nodes == 1:
                full, full_bump = build(name, grown, types_.get(name))
                empty = _small(name, 1)
                step = _filler(empty, full)
                if step is not None:
                    growth.append(step)
                    if full_bump is not None and full_bump[0] is full:  # the root list or dict itself
                        full_bump = (empty, full_bump[1])
                    elif full_bump is not None and hasattr(full, "__dict__") and full_bump[0] is vars(full):
                        full_bump = (vars(empty), full_bump[1])
                    created.setdefault((name, grown), []).append((empty, full_bump))
                    return empty
            pool = created.setdefault((name, nodes), [])
            if reuse and pool:
                value, bump = pool[-1]
            else:
                value, bump = build(name, nodes, types_.get(name))
                pool.append((value, bump))
            if reuse and chg and bump is not None:
                bumps.append(bump)
            return value

        for index, type_index, nodes, reuse, chg, grown in call["a"]:
            arguments[index] = value_for(type_index, nodes, reuse, chg, grown)
        returned = 7
        if call.get("r"):
            returned = value_for(*call["r"])
        steps.append((call["f"], tuple(arguments), returned, tuple(bumps), tuple(growth)))
    return steps


def _filler(empty, full):
    """(method, argument) that makes ``empty`` hold what ``full`` holds, or None for a kind not handled."""
    if type(full) is list and type(empty) is list:
        return empty.extend, list(full)
    if type(full) is dict and type(empty) is dict:
        return empty.update, dict(full)
    if hasattr(full, "__dict__") and hasattr(empty, "__dict__") and type(full) is type(empty):
        return vars(empty).update, dict(vars(full))
    return None


APP = '''\
"""Generated by evaluation/rich_scaled.py: a scaled copy of the rich benchmark (do not edit).

{count} calls of {functions} functions, the values of the rich run (types, sizes, roles) at 1/{divisor}.
"""

import time

import rich_scaled

SPEC = rich_scaled.load({spec})
_ret = None
_grow = ()

{definitions}
FUNCTIONS = [{names}]
STEPS = rich_scaled.prepare(SPEC)

_started = time.perf_counter()
for index, arguments, _ret, bumps, _grow in STEPS:
    for container, key in bumps:
        container[key] += 1
    FUNCTIONS[index](*arguments)
_seconds = time.perf_counter() - _started
open("loop_seconds.txt", "w").write(repr(_seconds))  # the time of the calls alone, for the benchmark
'''


def generate(out: Path, spec_path: Path | None = None) -> Path:
    spec = load(spec_path or SPEC_PATH)
    definitions = []
    for number, function in enumerate(spec["functions"]):
        definitions.append(
            f"def f{number}({', '.join(function['params'])}):\n"
            "    for _method, _argument in _grow:\n        _method(_argument)\n    return _ret\n\n"
        )
    text = APP.format(
        count=len(spec["calls"]), functions=len(spec["functions"]), divisor=spec["divisor"], spec=repr(str(spec_path)) if spec_path else "",
        definitions="\n".join(definitions), names=", ".join(f"f{number}" for number in range(len(spec["functions"]))),
    )
    out.mkdir(parents=True, exist_ok=True)
    target = out / "app.py"
    if not target.exists() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8", newline="\n")
    return target


# --------------------------------------------------------------------------------------
# Extracting the spec from a recorded run
# --------------------------------------------------------------------------------------

def _parameters(root: Path, function: str, cache: dict) -> list[str] | None:
    path, _, qualname = function.partition(":")
    tree = cache.get(path)
    if tree is None:
        try:
            tree = cache[path] = ast.parse((root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            tree = cache[path] = False
    if not tree:
        return None
    scope = tree
    for part in qualname.split("."):
        if part == "<locals>":
            continue
        for node in ast.walk(scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == part:
                scope = node
                break
        else:
            return None
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    arguments = scope.args
    names = [a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
    if arguments.vararg:
        names.append(arguments.vararg.arg)
    if arguments.kwarg:
        names.append(arguments.kwarg.arg)
    return names


def _tally(frame_id: str, function: str, calls: dict, by_number: dict) -> collections.Counter:
    """What one call contributes: its function, and the nodes and walks of its values by type and role."""
    tally = collections.Counter()
    tally[("call", function)] += 1
    for number in calls.get(frame_id, ()):
        row = by_number[number]
        tally[("type", row[2])] += row[3]
        tally[("role", row[4])] += row[3]
        tally[("walks", row[2])] += 1
    return tally


def _best_offset(recorded: list, calls: dict, by_number: dict, divisor: int) -> int:
    """Which of the ``divisor`` possible systematic samples is closest to the whole run.

    The sample is every ``divisor``-th call; the first call taken can be any of the first
    ``divisor``. The one chosen is the one whose shares of nodes by type and by role, of walks by
    type and of calls by function differ least (sum of absolute differences) from the run's."""
    classes = [collections.Counter() for _ in range(divisor)]
    total = collections.Counter()
    for index, (frame_id, function) in enumerate(recorded):
        tally = _tally(frame_id, function, calls, by_number)
        classes[index % divisor].update(tally)
        total.update(tally)
    sums = collections.Counter()
    for key, value in total.items():
        sums[key[0]] += value
    best, best_score = 0, None
    for offset, sample in enumerate(classes):
        sample_sums = collections.Counter()
        for key, value in sample.items():
            sample_sums[key[0]] += value
        # The sample's total nodes and walks should be the run's divided by ``divisor``, too.
        score = 2.0 * sum(
            abs(sample_sums[kind] * divisor / max(sums[kind], 1) - 1) for kind in ("type", "walks")
        )
        for key in total.keys() | sample.keys():
            weight = 0.5 if key[0] == "call" else 1.0
            score += weight * abs(sample.get(key, 0) / max(sample_sums[key[0]], 1) - total.get(key, 0) / max(sums[key[0]], 1))
        if best_score is None or score < best_score:
            best, best_score = offset, score
    return best


def extract(prefix: str, database: str, root: str, divisor: int = 100, output: Path | None = None) -> dict:
    output = output or SPEC_PATH
    sys.path.insert(0, str(HERE))
    import repeated_walks

    walks, calls, comps = repeated_walks.load(prefix)
    by_number = {row[0]: row for row in walks}
    starts = {number: indices[0] for indices in calls.values() for number in indices}

    # How often a value was one an earlier call had already walked, and how often changed since.
    seen = collections.defaultdict(lambda: [0, 0, 0])  # (type, role) -> slots, reused, changed
    for indices in calls.values():
        slots = {}
        for number in indices:
            row = by_number[number]
            slots.setdefault((row[4], row[6]), row)
        for row in slots.values():
            stat = seen[(row[2], row[4])]
            stat[0] += 1
            if row[1] < indices[0]:
                stat[1] += 1
                stat[2] += row[8] != row[9]
    rates = {key: (stat[1] / stat[0], stat[2] / stat[1] if stat[1] else 0.0) for key, stat in seen.items()}

    names, name_index = [], {}

    def type_index(name: str) -> int:
        if name not in name_index:
            name_index[name] = len(names)
            names.append(name)
        return name_index[name]

    functions, function_index, parse_cache = [], {}, {}
    accumulators = collections.defaultdict(lambda: [0.0, 0.0])
    sampled = []
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        run = connection.execute("SELECT id FROM runs ORDER BY started_ns DESC LIMIT 1").fetchone()[0]
        recorded = connection.execute(
            "SELECT frame_id, function FROM call_records WHERE run_id = ? ORDER BY worker, ordinal", (run,)
        ).fetchall()
        position = len(recorded)
        offset = _best_offset(recorded, calls, by_number, divisor)
        for index, (frame_id, function) in enumerate(recorded):
            if index % divisor != offset:
                continue
            slots = {}
            for number in calls.get(frame_id, ()):
                row = by_number[number]
                slot = slots.setdefault((row[4], row[6]), {"entry": None, "exit": None, "row": row})
                slot[row[5]] = row
            params = _parameters(Path(root), function, parse_cache)
            if params is None:
                params = [name for (role, name) in slots if role != "return"]
            receiver_shared = bool(params) and params[0] in RECEIVERS and ("receiver", params[0]) not in slots
            if receiver_shared:
                params = params[1:]
            key = (function, receiver_shared)
            if key not in function_index:
                function_index[key] = len(functions)
                safe = [name if name.isidentifier() else f"p{index}" for index, name in enumerate(params)]
                functions.append({"name": function + (" (receiver shared)" if receiver_shared else ""), "params": safe})
            call = {"f": function_index[key], "a": [], "r": None}
            for (role, name), slot in slots.items():
                first = slot["entry"] or slot["exit"]
                kind = first[2]
                reuse_rate, change_rate = rates.get((kind, role), (0.0, 0.0))
                accumulator = accumulators[(kind, role)]
                accumulator[0] += reuse_rate
                reuse = accumulator[0] >= 1
                accumulator[0] -= reuse
                change = False
                if reuse:
                    accumulator[1] += change_rate
                    change = accumulator[1] >= 1
                    accumulator[1] -= change
                grown = slot["exit"][3] if slot["entry"] and slot["exit"] and slot["entry"][3] == 1 and slot["exit"][3] > 1 else 0
                entry = [type_index(kind), first[3], int(reuse), int(change), grown]
                if role == "return":
                    call["r"] = entry
                elif name in params:
                    call["a"].append([params.index(name)] + entry)
            sampled.append(call)
    types = {}
    for name, rows in comps.items():
        rows = sorted(rows, key=lambda item: item[0])
        picks = sorted({round(i * (len(rows) - 1) / (SAMPLES_PER_TYPE - 1)) for i in range(SAMPLES_PER_TYPE)}) if len(rows) > 1 else [0]
        types[name] = [[rows[i][0], rows[i][1]] for i in picks]
    totals = collections.Counter()
    for row in walks:
        totals[("role", row[4])] += row[3]
        totals[("type", row[2])] += row[3]
        totals["nodes"] += row[3]
    source = {
        "calls": position, "walks": len(walks), "nodes": totals["nodes"],
        "role_share": {key[1]: round(value / totals["nodes"], 4) for key, value in totals.items() if key != "nodes" and key[0] == "role"},
        "type_share": {
            key[1]: round(value / totals["nodes"], 4)
            for key, value in totals.most_common() if key != "nodes" and key[0] == "type"
        },
    }
    source["type_share"] = dict(list(source["type_share"].items())[:12])
    spec = {
        "divisor": divisor, "offset": offset, "calls_in_source": position, "source": source,
        "type_names": names, "functions": functions,
        "calls": sampled, "types": types,
        "rates": {f"{kind}/{role}": [round(a, 4), round(b, 4)] for (kind, role), (a, b) in sorted(rates.items())},
    }
    output.write_text(json.dumps(spec, separators=(",", ":")), encoding="utf-8")
    return spec


def main() -> int:
    arguments = sys.argv[1:]
    divisor = 100
    if "--divisor" in arguments:
        at = arguments.index("--divisor")
        divisor = int(arguments[at + 1])
        del arguments[at:at + 2]
    spec_path = None
    if "--spec" in arguments:
        at = arguments.index("--spec")
        spec_path = Path(arguments[at + 1])
        del arguments[at:at + 2]
    if arguments[:1] == ["generate"]:
        print(generate(Path(arguments[1]), spec_path))
        return 0
    if arguments[:1] == ["extract"]:
        spec = extract(arguments[1], arguments[2], arguments[3], divisor, spec_path)
        print(f"{len(spec['calls'])} calls, {len(spec['functions'])} functions, {len(spec['types'])} types")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
