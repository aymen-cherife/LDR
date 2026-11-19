"""Split a frame's observed wall time at Python execution boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CallTiming:
    started: int
    active_since: int | None = None
    parent: CallTiming | None = None
    active: int = 0
    children: int = 0

    def resume(self, at: int, parent: CallTiming | None) -> None:
        if self.active_since is None:
            self.active_since = at
            self.parent = parent

    def pause(self, at: int) -> None:
        if self.active_since is None:
            return
        duration = at - self.active_since
        self.active += duration
        if self.parent is not None:
            self.parent.children += duration
        self.active_since = None
        self.parent = None

    def snapshot(self, at: int, *, complete: bool) -> dict[str, float | bool]:
        elapsed = at - self.started
        # Event boundaries can only account for children observed by this recorder.
        children = min(self.active, self.children)
        return {
            "elapsed_ms": round(elapsed / 1_000_000, 2),
            "own_ms": round((self.active - children) / 1_000_000, 2),
            "children_ms": round(children / 1_000_000, 2),
            "suspended_ms": round((elapsed - self.active) / 1_000_000, 2),
            "complete": complete,
        }
