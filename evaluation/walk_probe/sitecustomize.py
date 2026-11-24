"""Measurement probe (developers only): one row per fingerprint walk, with the object's identity.

Loaded through PYTHONPATH in every Python process; does nothing unless LDR_PROBE_OUT is set.
Writes <LDR_PROBE_OUT>.<pid>, one JSON row per line:
  [walk_index, object_index, type, nodes, role, phase, name, function, token, previous_token, microseconds]
      one walk. object_index is the index of the first walk of the same object (same id; the
      object is kept alive, so an id is never reused). previous_token is the fingerprint of the
      previous walk of that object ("" for the first). microseconds is the time the walk took.
  ["call", address, [walk_index, ...]]
      the walks a call made, by the call's address (file:qualname#n, as in value_sizes).
  ["comp", walk_index, type, nodes, {kind: count}]
      what the nodes of one walked value were made of (the first 40 walks of a type, then every
      20th): objects, objects without an instance __dict__, dicts, lists, strings, ints, ...,
      and "fast_*" for items the walk reads in bulk. "chars" is the total length of strings.
It patches ldr.fingerprint and ldr.tracer in this process only.
"""
import atexit
import json
import os
from time import perf_counter_ns

OUT = os.environ.get("LDR_PROBE_OUT")

if OUT:
    import ldr.fingerprint as fp
    import ldr.tracer as tr
    import ldr.structure as st

    state = {"last": None, "n": 0, "file": None, "rows": []}
    objects = {}  # id -> (object, first walk index, last token)
    original = fp._fingerprint_details

    import hashlib
    import types as _types

    class CountingWalk(fp._Walk):
        """The walk, counting what kind of node each visit is (never used for a fingerprint)."""

        def __init__(self, budget, partial_ok):
            super().__init__(budget, partial_ok)
            self.kinds = {}
            self.chars = 0

        def count(self, name, amount=1):
            self.kinds[name] = self.kinds.get(name, 0) + amount

        def encode(self, value, out, depth=0):
            kind = type(value)
            if value is None:
                self.count("none")
            elif kind in (bool, int, float, complex, bytes, bytearray):
                self.count(kind.__name__)
            elif kind is str:
                self.count("str")
                self.chars += len(value)
            elif kind in (list, tuple, dict, set, frozenset):
                self.count(kind.__name__)
            elif kind in (type, _types.FunctionType, _types.BuiltinFunctionType, _types.MethodType):
                self.count("leaf")
            else:
                reading = fp.read(value)
                if reading is None:
                    self.count("leaf")
                elif any(base in kind.__mro__ for base in (dict, list, tuple, set, frozenset)):
                    self.count("obj_builtin")
                else:
                    self.count("obj")
                    # an object with no instance __dict__ (slots only): nothing a dict watcher sees
                    if st.instance_dict(value) is None:
                        self.count("obj_noslotsdict")
                    elif any("__slots__" in vars(klass) for klass in kind.__mro__ if klass is not object):
                        self.count("obj_slots_and_dict")
            super().encode(value, out, depth)

        def _fast_items(self, value, out, depth):
            done = super()._fast_items(value, out, depth)
            if done:
                self.count("fast_" + type(value[0]).__name__, len(value))
                if type(value[0]) is str:
                    self.chars += sum(map(len, value))
            return done

    sampled = {}

    def composition(value, nodes):
        """Kinds of nodes of one walked value, for the first 40 walks of a type and every 20th after."""
        label = type(value).__qualname__
        seen = sampled[label] = sampled.get(label, 0) + 1
        if seen > 40 and seen % 20:
            return None
        walk = CountingWalk(fp._effective(), True)
        try:
            walk.encode(value, hashlib.blake2b(digest_size=8))
        except Exception:
            return None
        if walk.visited != nodes:
            return None
        walk.kinds["chars"] = walk.chars
        return walk.kinds

    def probed(value, budget, partial_ok, sketch=False):
        started = perf_counter_ns()
        found = original(value, budget, partial_ok, sketch=sketch)
        elapsed = perf_counter_ns() - started
        kinds = None
        if fp.STATS.walked:
            kinds = composition(value, fp.STATS.walked)
        state["last"] = (value, found[0], fp.STATS.walked, kinds, elapsed // 1000)
        return found

    fp._fingerprint_details = probed
    note = tr.Recorder._note_walk

    def flush():
        if state["rows"]:
            if state["file"] is None:
                state["file"] = open(f"{OUT}.{os.getpid()}", "w", encoding="utf-8")
            state["file"].write("\n".join(json.dumps(row) for row in state["rows"]) + "\n")
            state["file"].flush()
            state["rows"] = []

    def noted(self, frame, phase, name):
        note(self, frame, phase, name)
        last = state["last"]
        if last is None:
            return
        value, token, nodes, kinds, micros = last
        state["last"] = None
        if kinds is not None:
            state["rows"].append(["comp", state["n"] + 1, type(value).__qualname__, nodes, kinds])
        state["n"] += 1
        known = objects.get(id(value))
        if known is None:
            objects[id(value)] = known = [value, state["n"], ""]
        previous = known[2]
        known[2] = token
        role = "return" if name is None else "receiver" if name in tr._RECEIVERS else "argument"
        code = frame.f_code
        state["rows"].append([
            state["n"], known[1], type(value).__qualname__, nodes, role, phase, name or "",
            f"{os.path.basename(code.co_filename)}:{code.co_qualname}", token, previous, micros,
        ])
        if len(state["rows"]) >= 5000:
            flush()

    frames = {}  # id(frame) -> walk numbers of the call in progress
    sent = tr.Recorder._send_sizes

    def noted_calls(self, frame, phase, name):
        before = state["n"]
        noted(self, frame, phase, name)
        if state["n"] != before:
            current = frames.get(id(frame))
            if current is None or (phase == "entry" and current[1]):
                current = frames[id(frame)] = ([], False)
            current[0].append(state["n"])
            if phase == "exit":
                frames[id(frame)] = (current[0], True)

    def sent_calls(self, frame, frame_id):
        sent(self, frame, frame_id)
        current = frames.pop(id(frame), None)
        if current:
            state["rows"].append(["call", frame_id, current[0]])

    tr.Recorder._note_walk = noted_calls
    tr.Recorder._send_sizes = sent_calls
    atexit.register(flush)
