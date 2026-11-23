"""LDR: runtime evidence for coding agents.

The package imports nothing when it is imported: the launcher and the child that runs a
program load only what they use. The public names below are loaded on first use.
"""

__version__ = "0.6.0"

__all__ = [
    "Node", "Recorder", "diff_tree", "mark_unstable", "node_hash",
    "render_run", "render_diff", "render_call_chain", "__version__",
]

_WHERE = {
    "Node": "node", "Recorder": "tracer",
    "diff_tree": "diff", "mark_unstable": "diff", "node_hash": "diff",
    "render_run": "render", "render_diff": "render", "render_call_chain": "render",
}


def __getattr__(name):
    module = _WHERE.get(name)
    if module is None:
        raise AttributeError(f"module 'ldr' has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
