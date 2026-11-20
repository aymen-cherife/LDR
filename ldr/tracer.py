"""Low-overhead function-boundary recording built on ``sys.monitoring``.

The normal tree has a hard budget. After that budget is exhausted LDR keeps
only lightweight live frames, so a late exception can still be promoted into
an actionable call chain instead of disappearing behind the cap.
"""

from __future__ import annotations

import contextvars
import inspect
import linecache
import os
import sys
from dataclasses import dataclass, field
from time import perf_counter_ns

from .fingerprint import REDACTED, STATS, fingerprint, fingerprint_details
from .node import Node, capture, capture_error, capture_state, is_secret_name, redact_source_line
from .redact import redact, same_text_policy, text_policy
from .timing import CallTiming


_RECEIVERS = ("self", "cls")


def fp_value(value, name: str | None = None) -> str:
    """Fingerprint a captured value; secret-named variables are never hashed."""
    if name is not None and is_secret_name(name):
        return REDACTED
    return fingerprint(value, partial_ok=name in _RECEIVERS)


@dataclass(slots=True)
class _Shadow:
    code: object
    fid: int
    frame_id: str = ""
    segment: str = ""
    seen: dict[str, int] | None = None
    children: list[Node] = field(default_factory=list)
    preserve_children: bool = False


_stack: contextvars.ContextVar[tuple[Node | _Shadow, ...]] = contextvars.ContextVar(
    "ldr_stack", default=()
)


def _mark_handled(node: Node) -> None:
    if node.error is not None:
        node.handled = True
    for child in node.children:
        _mark_handled(child)


MIN_PYTHON = (3, 12)


def python_version_message() -> str | None:
    """Why LDR cannot run on this interpreter, or None if it can."""
    if sys.version_info[:2] >= MIN_PYTHON and hasattr(sys, "monitoring"):
        return None
    return (
        f"LDR needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+. "
        f"You are on {sys.version_info[0]}.{sys.version_info[1]}."
    )


class Recorder:
    def __init__(
        self,
        scope: list[str],
        max_nodes: int = 5000,
        max_depth: int = 60,
        lines_for: set[str] | None = None,
        project_root: str | None = None,
        display_root: str | None = None,
        focus_frame: str | None = None,
        max_state_steps: int = 200,
        max_calls_per_function: int = 8,
        deep_functions: list[str] | None = None,
        boundary_modules: list[str] | None = None,
    ):
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1")
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if max_state_steps < 1:
            raise ValueError("max_state_steps must be at least 1")
        if max_calls_per_function < 1:
            raise ValueError("max_calls_per_function must be at least 1")
        self.scope = [os.path.realpath(p) for p in scope]
        self.project_root = os.path.realpath(project_root or os.getcwd())
        self.display_root = os.path.realpath(display_root or os.getcwd())
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.lines_for = lines_for or set()
        self.focus_frame = focus_frame
        self.frame_prefix = ""
        self.call_stream = None
        self._immutable_inputs = {}
        self.deep_functions = frozenset(deep_functions or ())
        self.boundary_modules = tuple(boundary_modules or ())
        self.boundaries = None
        self._deep_codes: set[object] = set()
        self._global_lines = False
        self._timings: dict[int, CallTiming] = {}
        self.max_state_steps = max_state_steps
        self.max_calls_per_function = max_calls_per_function
        self.inspection_matches = 0
        self.roots: list[Node] = []
        self.suspended: dict[int, Node | _Shadow] = {}
        self.n_nodes = 0
        self.dropped_nodes = 0
        self.sampled_nodes = 0
        self.truncated = False
        self._active = False
        self._tool_id: int | None = None
        self._scope_cache: dict[str, bool] = {}
        self._display_cache: dict[str, str] = {}
        self._source_cache: dict[str, str] = {}
        self._code_cache: dict[object, tuple[int, str, str]] = {}
        self._suppressed_codes: set[object] = set()
        self._synthetic_exceptions: dict[int, list[Node]] = {}
        self._synthetic_objects: dict[int, BaseException] = {}
        self._synthetic_order: list[int] = []
        self._invocation_counts: dict[str, int] = {}
        # Address state: calls with no recorded caller count under the current
        # test root (or the run, outside pytest).
        self._root_counts: dict[str, int] = {}
        self._test_root = ""
        self._test_code = None
        self._registered_events: list[int] = []

    _NOISE = ("<genexpr>", "<listcomp>", "<dictcomp>", "<setcomp>", "<lambda>", "<module>")

    def _skip(self, code) -> bool:
        return code.co_name in self._NOISE or not (code.co_flags & inspect.CO_OPTIMIZED)

    def _in_scope(self, filename: str) -> bool:
        if not filename or filename.startswith("<"):
            return False
        cached = self._scope_cache.get(filename)
        if cached is not None:
            return cached
        candidate = os.path.realpath(filename)
        if f"{os.sep}site-packages{os.sep}" in candidate:
            self._scope_cache[filename] = False
            return False
        for root in self.scope:
            try:
                if os.path.commonpath((candidate, root)) == root:
                    self._scope_cache[filename] = True
                    return True
            except ValueError:
                continue
        self._scope_cache[filename] = False
        return False

    def _display_path(self, filename: str) -> str:
        cached = self._display_cache.get(filename)
        if cached is not None:
            return cached
        absolute = os.path.realpath(filename)
        try:
            if os.path.commonpath((absolute, self.display_root)) == self.display_root:
                result = os.path.relpath(absolute, self.display_root).replace(os.sep, "/")
                self._display_cache[filename] = result
                return result
        except ValueError:
            pass
        self._display_cache[filename] = absolute
        return absolute

    def _source_id(self, filename: str) -> str:
        """Stable source identity, independent of the checkout directory."""
        cached = self._source_cache.get(filename)
        if cached is not None:
            return cached
        absolute = os.path.realpath(filename)
        try:
            if os.path.commonpath((absolute, self.project_root)) == self.project_root:
                result = os.path.relpath(absolute, self.project_root).replace(os.sep, "/")
                self._source_cache[filename] = result
                return result
        except ValueError:
            pass
        matching_roots = []
        for index, root in enumerate(self.scope):
            try:
                if os.path.commonpath((absolute, root)) == root:
                    matching_roots.append((len(root), index, root))
            except ValueError:
                continue
        if matching_roots:
            _, index, root = max(matching_roots)
            relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            label = os.path.basename(root.rstrip(os.sep)) or f"scope-{index}"
            result = f"@{label}/{relative}"
        else:
            # This should only occur for synthetic frames. It deliberately
            # remains display-only rather than pretending to be portable.
            result = absolute.replace(os.sep, "/")
        self._source_cache[filename] = result
        return result

    def _metadata(self, code, frame) -> tuple[str, str, int, str, str]:
        cached = self._code_cache.get(code)
        if cached is None:
            cached = (
                code.co_firstlineno,
                self._display_path(code.co_filename),
                self._source_id(code.co_filename),
            )
            self._code_cache[code] = cached
        line, file, source_id = cached
        module = str(frame.f_globals.get("__name__", ""))
        callsite = ""
        caller = frame.f_back
        if caller is not None and caller.f_code.co_filename and not caller.f_code.co_filename.startswith("<"):
            callsite = f"{self._display_path(caller.f_code.co_filename)}:{caller.f_lineno}"
        return source_id, module, line, file, callsite

    def _arguments(self, code, frame, *, detailed: bool = False) -> dict[str, str]:
        names = self._argument_names(code)
        return {
            name: (capture_state if detailed else capture)(frame.f_locals[name], name)
            for name in names
            if name in frame.f_locals and name not in ("self", "cls")
        }

    def _argument_fingerprints(self, code, frame, *, entering=False) -> dict[str, str]:
        """Fingerprint each argument, reusing proven stable entry digests at exit."""
        walked = STATS.nodes
        result: dict[str, str] = {}
        locals_ = frame.f_locals
        remember = entering and self.call_stream is not None
        policy = text_policy() if remember else None
        reusable = {}
        saved = self._immutable_inputs.pop(id(frame), None)
        saved_policy, saved_values = saved if saved is not None else (None, {})
        for name in self._argument_names(code):
            if name not in locals_:
                continue
            value = locals_[name]
            previous = saved_values.get(name)
            if (
                previous is not None
                and value is previous[0]
                and same_text_policy(saved_policy)
            ):
                result[name] = previous[1]
            elif remember:
                if is_secret_name(name):
                    result[name] = REDACTED
                    continue
                token, immutable = fingerprint_details(value, partial_ok=name in _RECEIVERS)
                if immutable:
                    # Keep the original object alive for the identity check at exit.
                    reusable[name] = (value, token)
                result[name] = token
            else:
                result[name] = fp_value(value, name)
        if reusable and same_text_policy(policy):
            self._immutable_inputs[id(frame)] = (policy, reusable)
        self._charge(code, walked)
        return result

    def _charge(self, code, walked_before: int) -> None:
        """Record the fingerprint work done since ``walked_before`` against this function."""
        spent = STATS.nodes - walked_before
        if spent and self.call_stream is not None:
            self.call_stream.add_cost(
                f"{self._source_id(code.co_filename)}:{code.co_qualname}", spent
            )

    @staticmethod
    def _argument_names(code) -> list[str]:
        names = list(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
        index = code.co_argcount + code.co_kwonlyargcount
        if code.co_flags & inspect.CO_VARARGS:
            names.append(code.co_varnames[index])
            index += 1
        if code.co_flags & inspect.CO_VARKEYWORDS:
            names.append(code.co_varnames[index])
        return names

    def _mutable_argument_evidence(self, code, frame) -> dict[str, tuple[int, str]]:
        """Capture bounded entry state for common caller-owned mutable values."""
        evidence: dict[str, tuple[int, str]] = {}
        for name in self._argument_names(code):
            if name in ("self", "cls") or name not in frame.f_locals:
                continue
            value = frame.f_locals[name]
            if type(value) not in (dict, list, set, bytearray):
                continue
            evidence[name] = (id(value), capture_state(value, name))
        return evidence

    @staticmethod
    def _finish_mutable_argument_evidence(entry: Node, frame) -> None:
        """Record only in-place changes visible through the original object."""
        for name, (object_id, before) in entry.__dict__.get(
            "_mutable_argument_evidence", {}
        ).items():
            if name not in frame.f_locals:
                continue
            value = frame.f_locals[name]
            if id(value) != object_id:
                continue
            after = capture_state(value, name)
            if before != after:
                entry.input_changes[name] = {"before": before, "after": after}

    def begin_test(self, root: str, code=None, partition: str = "") -> None:
        """Make ``root`` (a pytest node id) the root of the addresses that follow.

        ``code`` is the test function's code object: its own frame is the root
        itself. ``partition`` keys the per-function rolling digests.
        """
        self._test_root = root
        self._test_code = code
        self._root_counts = {}
        self.frame_prefix = partition

    @staticmethod
    def _entry_segment(entry: Node | _Shadow | None) -> str:
        if entry is None:
            return ""
        return entry.segment if isinstance(entry, _Shadow) else entry.__dict__.get("_segment", "")

    def _next_frame_id(self, code, parent: Node | _Shadow | None) -> tuple[str, str, int]:
        """Address ``file:qualname#n`` along the call path, without the definition line.

        ``n`` is the nth call of that function under the same parent. The
        second value is the call's own path segment; the third counts the
        function's calls across the whole run, for sampling.
        """
        key = f"{self._source_id(code.co_filename)}:{code.co_qualname}"
        ordinal = self._invocation_counts.get(key, 0) + 1
        self._invocation_counts[key] = ordinal
        if parent is None:
            seen = self._root_counts
            base = self._test_root
        else:
            if isinstance(parent, _Shadow):
                if parent.seen is None:
                    parent.seen = {}
                seen = parent.seen
            else:
                seen = parent.__dict__.setdefault("_seen", {})
            base = parent.frame_id
        number = seen.get(key, 0) + 1
        seen[key] = number
        segment = f"{key}#{number}"
        if parent is None and code is self._test_code and number == 1 and base:
            return base, segment, ordinal
        return (f"{base}/{segment}" if base else segment), segment, ordinal

    def _sample_repeated_call(self, ordinal: int) -> bool:
        """Keep early calls and exponentially spaced samples from hot functions."""
        return ordinal > self.max_calls_per_function and ordinal & (ordinal - 1) != 0

    def _new_node(self, code, frame, frame_id: str, *, detailed: bool = False) -> Node:
        source_id, module, line, file, callsite = self._metadata(code, frame)
        try:
            arguments = self._arguments(code, frame, detailed=detailed)
        except Exception:
            arguments = {}
        try:
            argument_fps = self._argument_fingerprints(code, frame, entering=True)
        except Exception:
            argument_fps = {}
        node = Node(
            name=code.co_qualname,
            in_=arguments,
            in_fp=argument_fps,
            module=module,
            file=file,
            source_id=source_id,
            line=line,
            callsite=callsite,
            frame_id=frame_id,
        )
        node.__dict__["_fid"] = id(frame)
        node.__dict__["_code"] = code
        node.__dict__["_mutable_argument_evidence"] = self._mutable_argument_evidence(
            code, frame
        )
        return node

    def _new_shadow(
        self,
        code,
        frame,
        frame_id: str,
        *,
        preserve_children: bool = False,
    ) -> _Shadow:
        return _Shadow(
            code=code,
            fid=id(frame),
            frame_id=frame_id,
            preserve_children=preserve_children,
        )

    @staticmethod
    def _entry_fid(entry: Node | _Shadow) -> int:
        return entry.fid if isinstance(entry, _Shadow) else entry.__dict__.get("_fid", -1)

    @staticmethod
    def _entry_frame_id(entry: Node | _Shadow) -> str:
        return entry.frame_id

    def _attach(self, node: Node, stack: tuple[Node | _Shadow, ...]) -> None:
        if not stack:
            self.roots.append(node)
        else:
            stack[-1].children.append(node)

    def _focus_matches(self, frame_id: str, segment: str) -> bool:
        if not self.focus_frame:
            return False
        if self.focus_frame.endswith("#*"):
            return segment.startswith(self.focus_frame[:-1])
        return frame_id == self.focus_frame

    def _timing_parent(self, frame):
        caller = frame.f_back
        while caller is not None:
            parent = self._timings.get(id(caller))
            if parent is not None and parent.active_since is not None:
                return parent
            caller = caller.f_back
        return None

    def _start_capture(self, code, frame, frame_id, at, node=None):
        timing = CallTiming(at)
        timing.resume(at, self._timing_parent(frame))
        self._timings[id(frame)] = timing
        if self.call_stream is None:
            return
        inputs = (
            node.in_fp
            if node is not None
            else self._argument_fingerprints(code, frame, entering=True)
        )
        preview = None
        if self.call_stream.keeping_details:
            preview = node.in_ if node is not None else self._arguments(code, frame)
        identity = f"{self._source_id(code.co_filename)}:{code.co_qualname}"
        function = f"{self.frame_prefix}{identity}"
        deep = bool(self.deep_functions) and (
            code.co_name in self.deep_functions
            or code.co_qualname in self.deep_functions
            or identity in self.deep_functions
        )
        if deep and code not in self._deep_codes:
            if not self._global_lines:
                mon = sys.monitoring
                mon.set_local_events(self._tool_id, code, mon.events.LINE)
            self._deep_codes.add(code)
        self.call_stream.start(frame, frame_id, function, inputs, preview, deep=deep)

    def _finish_timing(self, frame, at):
        timing = self._timings.pop(id(frame), None)
        if timing is not None:
            timing.pause(at)
            return timing.snapshot(at, complete=True)
        return None

    @staticmethod
    def _set_node_timing(node, timing):
        if timing is not None:
            node.timing = timing
            node.ms = timing["elapsed_ms"]

    def _on_start(self, code, offset):
        mon = sys.monitoring
        if not self._in_scope(code.co_filename):
            if self.boundaries is None or self._skip(code):
                return mon.DISABLE
            self._start_boundary(code, sys._getframe(1))
            # This code can be called directly from the project on another invocation.
            return
        if self._skip(code):
            return mon.DISABLE
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        at = perf_counter_ns()
        stack = _stack.get()
        parent = stack[-1] if stack else None
        frame_id, segment, ordinal = self._next_frame_id(code, parent)
        if code in self._suppressed_codes:
            self._start_capture(code, frame, frame_id, at)
            return
        focused = self._focus_matches(frame_id, segment)
        direct_focus = bool(
            self.focus_frame
            and parent is not None
            and self._focus_matches(self._entry_frame_id(parent), self._entry_segment(parent))
        )
        sampled_repeat = self._sample_repeated_call(ordinal) and not (focused or direct_focus)
        if sampled_repeat:
            # Preserve budget without disabling the code object: later
            # power-of-two samples can still expose an anomalous invocation.
            self.sampled_nodes += 1
            self.truncated = True
            entry = self._new_shadow(code, frame, frame_id, preserve_children=True)
            entry.segment = segment
        elif (
            (self.n_nodes < self.max_nodes and len(stack) < self.max_depth)
            or focused
            or direct_focus
        ):
            entry: Node | _Shadow = self._new_node(
                code,
                frame,
                frame_id,
                detailed=focused,
            )
            entry.__dict__["_segment"] = segment
            self._attach(entry, stack)
            self.n_nodes += 1
            if focused:
                self.inspection_matches += 1
        else:
            self.dropped_nodes += 1
            self.truncated = True
            active_same_code = any(
                isinstance(item, _Shadow) and item.code is code
                or isinstance(item, Node) and item.__dict__.get("_code") is code
                for item in stack
            )
            async_or_generator = bool(
                code.co_flags
                & (inspect.CO_COROUTINE | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR)
            )
            if (
                self.n_nodes >= self.max_nodes
                and not active_same_code
                and not async_or_generator
            ):
                # Disable start/return callbacks for this hot code object.
                # The global RAISE event remains active and reconstructs the
                # scoped Python frame chain if a later invocation fails.
                self._suppressed_codes.add(code)
                if self.call_stream is not None:
                    self._start_capture(code, frame, frame_id, at)
                    return
                return mon.DISABLE
            entry = self._new_shadow(code, frame, frame_id)
            entry.segment = segment
        self._start_capture(code, frame, frame_id, at, entry if isinstance(entry, Node) else None)
        _stack.set(stack + (entry,))

    def _start_boundary(self, code, frame):
        caller = frame.f_back
        if caller is None or not self._in_scope(caller.f_code.co_filename):
            return
        module = frame.f_globals.get("__name__")
        if type(module) is not str or not any(
            module == selected or module.startswith(selected + ".")
            for selected in self.boundary_modules
        ):
            return
        address = self.call_stream.frame_address(caller)
        source = self._display_path(caller.f_code.co_filename)
        site = {
            "file": redact(source)[0],
            "line": caller.f_lineno,
            "frame_id": redact(address)[0] if address is not None else None,
        }
        locals_ = frame.f_locals
        bindings = (
            (name, locals_[name]) for name in self._argument_names(code) if name in locals_
        )
        self.boundaries.start(id(frame), module, code.co_qualname, site, bindings)

    @staticmethod
    def _state_locals(frame) -> dict[str, str]:
        captured: dict[str, str] = {}
        for name, value in list(frame.f_locals.items())[:64]:
            if name in ("self", "cls") or name.startswith("__"):
                continue
            captured[name] = capture_state(value, name)
        return captured

    @staticmethod
    def _state_changes(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
        changes = []
        for name in sorted(set(before) | set(after)):
            left = before.get(name, "<unset>")
            right = after.get(name, "<deleted>")
            if left != right:
                changes.append({"name": name, "before": left, "after": right})
        return changes

    def _append_state_step(
        self,
        entry: Node,
        code,
        line: int,
        before: dict[str, str],
        after: dict[str, str],
    ) -> None:
        if len(entry.state_steps) >= self.max_state_steps:
            entry.state_truncated = True
            return
        statement = redact_source_line(linecache.getline(code.co_filename, line).strip())
        entry.state_steps.append(
            {
                "line": line,
                "statement": statement,
                "changes": self._state_changes(before, after),
            }
        )

    def _capture_state_transition(self, code, frame, next_line: int | None) -> None:
        if not self.focus_frame:
            return
        matched = self._matching_stack(frame)
        if matched is None:
            return
        _, entry = matched
        if not isinstance(entry, Node) or not self._focus_matches(
            entry.frame_id, self._entry_segment(entry)
        ):
            return
        current = self._state_locals(frame)
        previous = entry.__dict__.get("_state_locals")
        previous_line = entry.__dict__.get("_state_line")
        if previous is not None and previous_line is not None:
            self._append_state_step(entry, code, previous_line, previous, current)
        entry.__dict__["_state_locals"] = current
        entry.__dict__["_state_line"] = next_line

    def _on_line(self, code, line_number):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return sys.monitoring.DISABLE
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        if self.call_stream is not None and self.call_stream.store.capture_lines:
            self.call_stream.record_line(id(frame), line_number)
        if code in self._deep_codes:
            self.call_stream.record_deep(frame, line_number, "line")
        if self.focus_frame:
            self._capture_state_transition(code, frame, line_number)

    def _promote_suppressed(self, frame, exc: BaseException) -> None:
        exception_id = id(exc)
        if exception_id in self._synthetic_exceptions:
            return

        scoped_frames = []
        current = frame
        while current is not None and len(scoped_frames) < self.max_depth:
            code = current.f_code
            if self._in_scope(code.co_filename) and not self._skip(code):
                scoped_frames.append(current)
            current = current.f_back
        if not scoped_frames:
            return

        stack = _stack.get()
        active = {self._entry_fid(entry): entry for entry in stack}
        parent: Node | _Shadow | None = None
        build_frames = scoped_frames
        for index, candidate in enumerate(scoped_frames):
            matched = active.get(id(candidate))
            if matched is not None:
                parent = matched
                build_frames = scoped_frames[:index]
                break

        error = capture_error(exc)
        created: list[Node] = []
        for candidate in reversed(build_frames):
            code = candidate.f_code
            try:
                arguments = self._arguments(code, candidate)
            except Exception:
                arguments = {}
            source_id, module, line, file, callsite = self._metadata(code, candidate)
            node = Node(
                name=code.co_qualname,
                in_=arguments,
                error=error,
                handled=True,
                module=module,
                file=file,
                source_id=source_id,
                line=line,
                callsite=callsite,
                frame_id="",
            )
            node.__dict__["_exc_id"] = exception_id
            if parent is None:
                self.roots.append(node)
            else:
                parent.children.append(node)
            parent = node
            created.append(node)
        if created:
            self._synthetic_exceptions[exception_id] = created
            # Retain the object so CPython cannot reuse its id for a later,
            # unrelated exception before final status classification.
            self._synthetic_objects[exception_id] = exc
            self._synthetic_order.append(exception_id)

    def _matching_stack(self, frame) -> tuple[tuple[Node | _Shadow, ...], Node | _Shadow] | None:
        stack = _stack.get()
        if not stack:
            return None
        fid = id(frame)
        for index in range(len(stack) - 1, -1, -1):
            entry = stack[index]
            if self._entry_fid(entry) == fid:
                if index != len(stack) - 1:
                    self.truncated = True
                return stack[:index], entry
        return None

    def _close(self, code, frame, timing, *, error=None, return_value=None):
        matched = self._matching_stack(frame)
        if matched is None:
            self._immutable_inputs.pop(id(frame), None)
            return
        parent_stack, entry = matched
        node_retained = isinstance(entry, Node)
        stream = self.call_stream
        out = out_fp = None
        if error is None:
            if node_retained or (stream is not None and stream.keeping_details):
                out = capture(return_value)
            if node_retained or stream is not None:
                walked = STATS.nodes
                out_fp = fp_value(return_value)
                self._charge(code, walked)
        if isinstance(entry, Node) and self._focus_matches(
            entry.frame_id, self._entry_segment(entry)
        ):
            self._capture_state_transition(code, frame, None)
        if isinstance(entry, Node):
            self._finish_mutable_argument_evidence(entry, frame)
        self.suspended.pop(self._entry_fid(entry), None)
        _stack.set(parent_stack)
        if self.call_stream is not None:
            self.call_stream.close(
                frame, self._argument_fingerprints(code, frame), out_fp, error, out, timing, return_value,
            )

        if isinstance(entry, _Shadow):
            if error is None:
                for child in entry.children:
                    _mark_handled(child)
                    if entry.preserve_children:
                        self._attach(child, parent_stack)
                return
            try:
                arguments = self._arguments(code, frame)
            except Exception:
                arguments = {}
            source_id, module, line, file, callsite = self._metadata(code, frame)
            promoted = Node(
                name=code.co_qualname,
                in_=arguments,
                error=error,
                module=module,
                file=file,
                source_id=source_id,
                line=line,
                callsite=callsite,
                children=entry.children,
                frame_id=entry.frame_id,
            )
            self._set_node_timing(promoted, timing)
            self._attach(promoted, parent_stack)
            return

        self._set_node_timing(entry, timing)
        entry.out = out
        entry.out_fp = out_fp
        entry.error = error
        if error is None:
            for child in entry.children:
                _mark_handled(child)

    def _on_return(self, code, offset, retval):
        if not self._in_scope(code.co_filename):
            if self.boundaries is None or self._skip(code):
                return sys.monitoring.DISABLE
            self.boundaries.close(id(sys._getframe(1)), retval)
            return
        if self._skip(code):
            return sys.monitoring.DISABLE
        frame = sys._getframe(1)
        timing = self._finish_timing(frame, perf_counter_ns())
        if code in self._deep_codes:
            self.call_stream.record_deep(frame, None, "return")
        if code in self._suppressed_codes:
            if self.call_stream is not None:
                preview = capture(retval) if self.call_stream.keeping_details else None
                self.call_stream.close(
                    frame, self._argument_fingerprints(code, frame),
                    fp_value(retval), None, preview, timing, retval,
                )
                return
            return sys.monitoring.DISABLE
        self._close(code, frame, timing, return_value=retval)

    def _on_yield(self, code, offset, retval):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return sys.monitoring.DISABLE
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        timing = self._timings.get(id(frame))
        if timing is not None:
            timing.pause(perf_counter_ns())
        if code in self._deep_codes:
            self.call_stream.record_deep(frame, None, "yield")
        matched = self._matching_stack(frame)
        if matched is None:
            return
        parent_stack, entry = matched
        self.suspended[id(frame)] = entry
        _stack.set(parent_stack)

    def _on_resume(self, code, offset, _exception=None):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        fid = id(frame)
        if code in self._deep_codes:
            self.call_stream.resume_deep(frame)
        timing = self._timings.get(fid)
        if timing is not None:
            timing.resume(perf_counter_ns(), self._timing_parent(frame))
        entry = self.suspended.pop(fid, None)
        if entry is not None:
            _stack.set(_stack.get() + (entry,))

    def _on_unwind(self, code, offset, exc):
        if not self._in_scope(code.co_filename):
            if self.boundaries is not None and not self._skip(code):
                self.boundaries.close(id(sys._getframe(1)), exc, raised=True)
            return
        if self._skip(code):
            return
        frame = sys._getframe(1)
        timing = self._finish_timing(frame, perf_counter_ns())
        if code in self._deep_codes:
            self.call_stream.record_deep(frame, None, "unwind")
        if code in self._suppressed_codes:
            self._promote_suppressed(frame, exc)
            if self.call_stream is not None:
                self.call_stream.close(
                    frame, self._argument_fingerprints(code, frame),
                    None, capture_error(exc), None, timing,
                )
            return
        self._close(code, frame, timing, error=capture_error(exc))

    def _on_raise(self, code, offset, exc):
        if code not in self._suppressed_codes:
            return
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        self._promote_suppressed(frame, exc)

    def _claim_tool_id(self) -> int:
        mon = sys.monitoring
        candidates = [
            getattr(mon, "PROFILER_ID", 2),
            3,
            4,
            getattr(mon, "OPTIMIZER_ID", 5),
        ]
        errors = []
        for tool_id in dict.fromkeys(candidates):
            try:
                mon.use_tool_id(tool_id, "ldr")
                return tool_id
            except ValueError as exc:
                errors.append(str(exc))
        raise RuntimeError(
            "LDR could not acquire a sys.monitoring slot; stop another active profiler "
            "or coverage/debugging tool first. " + "; ".join(errors)
        )

    def start(self) -> None:
        unsupported = python_version_message()
        if unsupported:
            raise RuntimeError(unsupported)
        if self.deep_functions and self.call_stream is None:
            raise RuntimeError("deep capture requires SQLite trace storage")
        if self.boundary_modules:
            if self.call_stream is None or self.call_stream.boundaries is None:
                raise RuntimeError("boundary capture requires SQLite trace storage")
            self.boundaries = self.call_stream.boundaries
        mon = sys.monitoring
        tool_id = self._claim_tool_id()
        self._tool_id = tool_id
        events = mon.events
        callbacks = {
            events.PY_START: self._on_start,
            events.PY_RETURN: self._on_return,
            events.PY_YIELD: self._on_yield,
            events.PY_RESUME: self._on_resume,
            events.PY_THROW: self._on_resume,
            events.PY_UNWIND: self._on_unwind,
            events.RAISE: self._on_raise,
        }
        self._global_lines = bool(
            self.focus_frame
            or (self.call_stream is not None and self.call_stream.store.capture_lines)
        )
        if self._global_lines or self.deep_functions:
            callbacks[events.LINE] = self._on_line
        try:
            for event, callback in callbacks.items():
                mon.register_callback(tool_id, event, callback)
            mon.set_events(
                tool_id,
                sum(event for event in callbacks if self._global_lines or event != events.LINE),
            )
            self._registered_events = list(callbacks)
        except Exception:
            try:
                mon.set_events(tool_id, 0)
                for event in callbacks:
                    mon.register_callback(tool_id, event, None)
            finally:
                mon.free_tool_id(tool_id)
                self._tool_id = None
            raise
        self._active = True

    def finalize(self, success: bool, escaped_exception: BaseException | None = None) -> None:
        if success:
            for root in self.roots:
                _mark_handled(root)
            return
        exception_id = id(escaped_exception) if escaped_exception is not None else None
        if exception_id in self._synthetic_exceptions:
            # Pytest catches the test exception and reports failure via its
            # exit status. Bind the final trace to that exact report object so
            # expected module probes and other isolated AttributeErrors cannot
            # outrank a late failure promoted after the node budget.
            for root in self.roots:
                _mark_handled(root)
            for node in self._synthetic_exceptions[exception_id]:
                node.handled = False
            return
        has_regular_unhandled = any(
            node.error is not None and not node.handled
            for root in self.roots
            for node in root.walk()
        )
        if (
            exception_id not in self._synthetic_exceptions
            and self._synthetic_order
            and not has_regular_unhandled
        ):
            exception_id = self._synthetic_order[-1]
        for node in self._synthetic_exceptions.get(exception_id, []):
            node.handled = False

    def stop(self) -> None:
        if not self._active or self._tool_id is None:
            return
        mon = sys.monitoring
        tool_id = self._tool_id
        mon.set_events(tool_id, 0)
        if not self._global_lines:
            for code in self._deep_codes:
                mon.set_local_events(tool_id, code, 0)
        self._deep_codes.clear()
        for event in self._registered_events:
            mon.register_callback(tool_id, event, None)
        mon.free_tool_id(tool_id)
        self._active = False
        self._tool_id = None
        self._registered_events.clear()
        self._suppressed_codes.clear()
        _stack.set(())
        stopped = perf_counter_ns()
        for fid, entry in self.suspended.items():
            if isinstance(entry, Node) and entry.out is None and entry.error is None:
                entry.out = "<suspended>"
                timing = self._timings.get(fid)
                if timing is not None:
                    self._set_node_timing(entry, timing.snapshot(stopped, complete=False))
        self.suspended.clear()
        self._timings.clear()
        self._immutable_inputs.clear()
