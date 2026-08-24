"""Low-overhead function-boundary recording built on ``sys.monitoring``.

The normal tree has a hard budget. After that budget is exhausted LDR keeps
only lightweight live frames, so a late exception can still be promoted into
an actionable call chain instead of disappearing behind the cap.
"""

from __future__ import annotations

import contextvars
import inspect
import os
import sys
import time
from dataclasses import dataclass, field

from .node import Node, capture, capture_error


@dataclass(slots=True)
class _Shadow:
    code: object
    fid: int
    children: list[Node] = field(default_factory=list)


_stack: contextvars.ContextVar[tuple[Node | _Shadow, ...]] = contextvars.ContextVar(
    "ldr_stack", default=()
)


def _mark_handled(node: Node) -> None:
    if node.error is not None:
        node.handled = True
    for child in node.children:
        _mark_handled(child)


class Recorder:
    def __init__(
        self,
        scope: list[str],
        max_nodes: int = 5000,
        max_depth: int = 60,
        lines_for: set[str] | None = None,
        project_root: str | None = None,
        display_root: str | None = None,
    ):
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1")
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self.scope = [os.path.realpath(p) for p in scope]
        self.project_root = os.path.realpath(project_root or os.getcwd())
        self.display_root = os.path.realpath(display_root or os.getcwd())
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        # Retained for API compatibility. Line tracing was never implemented
        # and is intentionally not advertised as a working feature.
        self.lines_for = lines_for or set()
        self.roots: list[Node] = []
        self.suspended: dict[int, Node | _Shadow] = {}
        self.n_nodes = 0
        self.dropped_nodes = 0
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
                result = os.path.relpath(absolute, self.display_root)
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

    def _arguments(self, code, frame) -> dict[str, str]:
        names = list(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
        index = code.co_argcount + code.co_kwonlyargcount
        if code.co_flags & inspect.CO_VARARGS:
            names.append(code.co_varnames[index])
            index += 1
        if code.co_flags & inspect.CO_VARKEYWORDS:
            names.append(code.co_varnames[index])
        return {
            name: capture(frame.f_locals[name], name)
            for name in names
            if name in frame.f_locals and name not in ("self", "cls")
        }

    def _new_node(self, code, frame) -> Node:
        source_id, module, line, file, callsite = self._metadata(code, frame)
        try:
            arguments = self._arguments(code, frame)
        except Exception:
            arguments = {}
        node = Node(
            name=code.co_qualname,
            in_=arguments,
            module=module,
            file=file,
            source_id=source_id,
            line=line,
            callsite=callsite,
        )
        node.__dict__["_t0"] = time.perf_counter()
        node.__dict__["_fid"] = id(frame)
        node.__dict__["_code"] = code
        return node

    def _new_shadow(self, code, frame) -> _Shadow:
        return _Shadow(code=code, fid=id(frame))

    @staticmethod
    def _entry_fid(entry: Node | _Shadow) -> int:
        return entry.fid if isinstance(entry, _Shadow) else entry.__dict__.get("_fid", -1)

    def _attach(self, node: Node, stack: tuple[Node | _Shadow, ...]) -> None:
        if not stack:
            self.roots.append(node)
        else:
            stack[-1].children.append(node)

    def _on_start(self, code, offset):
        mon = sys.monitoring
        if not self._in_scope(code.co_filename) or self._skip(code):
            return mon.DISABLE
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        stack = _stack.get()
        if self.n_nodes < self.max_nodes and len(stack) < self.max_depth:
            entry: Node | _Shadow = self._new_node(code, frame)
            self._attach(entry, stack)
            self.n_nodes += 1
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
                return mon.DISABLE
            entry = self._new_shadow(code, frame)
        _stack.set(stack + (entry,))

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

    def _close(self, code, out=None, error=None):
        try:
            frame = sys._getframe(2)
        except Exception:
            return
        matched = self._matching_stack(frame)
        if matched is None:
            return
        parent_stack, entry = matched
        self.suspended.pop(self._entry_fid(entry), None)
        _stack.set(parent_stack)

        if isinstance(entry, _Shadow):
            if error is None:
                for child in entry.children:
                    _mark_handled(child)
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
            )
            self._attach(promoted, parent_stack)
            return

        entry.ms = (time.perf_counter() - entry.__dict__.get("_t0", 0)) * 1000
        entry.out = out
        entry.error = error
        if error is None:
            for child in entry.children:
                _mark_handled(child)

    def _on_return(self, code, offset, retval):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return sys.monitoring.DISABLE
        if code in self._suppressed_codes:
            return sys.monitoring.DISABLE
        self._close(code, out=capture(retval))

    def _on_yield(self, code, offset, retval):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return sys.monitoring.DISABLE
        try:
            frame = sys._getframe(1)
        except Exception:
            return
        matched = self._matching_stack(frame)
        if matched is None:
            return
        parent_stack, entry = matched
        self.suspended[id(frame)] = entry
        _stack.set(parent_stack)

    def _on_resume(self, code, offset):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return
        try:
            fid = id(sys._getframe(1))
        except Exception:
            return
        entry = self.suspended.pop(fid, None)
        if entry is not None:
            _stack.set(_stack.get() + (entry,))

    def _on_unwind(self, code, offset, exc):
        if not self._in_scope(code.co_filename) or self._skip(code):
            return
        if code in self._suppressed_codes:
            try:
                frame = sys._getframe(1)
            except Exception:
                return
            self._promote_suppressed(frame, exc)
            return
        self._close(code, error=capture_error(exc))

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
        mon = sys.monitoring
        tool_id = self._claim_tool_id()
        self._tool_id = tool_id
        events = mon.events
        callbacks = {
            events.PY_START: self._on_start,
            events.PY_RETURN: self._on_return,
            events.PY_YIELD: self._on_yield,
            events.PY_RESUME: self._on_resume,
            events.PY_UNWIND: self._on_unwind,
            events.RAISE: self._on_raise,
        }
        try:
            for event, callback in callbacks.items():
                mon.register_callback(tool_id, event, callback)
            mon.set_events(tool_id, sum(callbacks))
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
        for event_name in ("PY_START", "PY_RETURN", "PY_YIELD", "PY_RESUME", "PY_UNWIND", "RAISE"):
            mon.register_callback(tool_id, getattr(mon.events, event_name), None)
        mon.free_tool_id(tool_id)
        self._active = False
        self._tool_id = None
        self._suppressed_codes.clear()
        _stack.set(())
        for entry in self.suspended.values():
            if isinstance(entry, Node) and entry.out is None and entry.error is None:
                entry.out = "<suspended>"
        self.suspended.clear()
