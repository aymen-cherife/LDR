"""Phase 2 rules 6 and 7: what a rerun needs so that the same code sees the same randomness.

Nothing here changes what the program computes. The hash seed is chosen only when the
user has not set one; the generators' states are read, never advanced, and restored only
when a rerun hands a record back.

Known gaps, stated in every run's output: ``os.urandom``, ``uuid``, ``time`` and the
network are not controlled, nor are instances made with ``default_rng()`` or
``RandomState`` or other libraries' generators. Fingerprint verification covers them.
"""

from __future__ import annotations

import base64
import importlib.abc
import importlib.machinery
import marshal
import random
import secrets
import sys
import zlib

SEED_SOURCE_ENV = "LDR_HASH_SEED_SOURCE"
GAPS = (
    "os.urandom, uuid, time and the network are not controlled; nor are generators made "
    "with numpy default_rng()/RandomState or other libraries' generators"
)


# --- rule 6: PYTHONHASHSEED

def choose_hash_seed(environ, recorded: str | None = None) -> tuple[str, str]:
    """The value for PYTHONHASHSEED and where it came from.

    The user's own value is kept. ``random`` and an unset variable mean "not chosen":
    a random one is picked, so that it can be recorded. A rerun passes the value
    recorded for the run it repeats.
    """
    if recorded is not None:
        return str(recorded), "recorded"
    value = environ.get("PYTHONHASHSEED")
    if value not in (None, "", "random"):
        return value, "user"
    return str(secrets.randbelow(2**32)), "chosen"


def hash_seed_status(environ, source: str | None) -> dict:
    """What the run records about its hash seed."""
    value = environ.get("PYTHONHASHSEED")
    if value in (None, "", "random"):
        return {"value": None, "source": "not set (iteration order of sets is not repeatable)"}
    return {"value": value, "source": source or "user"}


# --- rule 7: random, numpy and pytest-randomly

def capture_random() -> str:
    """The state of the ``random`` module, as compact text. Reading it does not advance it."""
    return base64.b64encode(zlib.compress(marshal.dumps(random.getstate()), 6)).decode("ascii")


def restore_random(record: str) -> None:
    version, internal, gauss = marshal.loads(zlib.decompress(base64.b64decode(record)))
    random.setstate((version, tuple(internal), gauss))


def _numpy_state(numpy) -> str:
    kind, keys, position, has_gauss, cached = numpy.random.get_state()
    return base64.b64encode(
        zlib.compress(marshal.dumps((kind, keys.tolist(), position, has_gauss, cached)), 6)
    ).decode("ascii")


def _restore_numpy(numpy, record: str) -> None:
    kind, keys, position, has_gauss, cached = marshal.loads(zlib.decompress(base64.b64decode(record)))
    numpy.random.set_state((kind, numpy.array(keys, dtype="uint32"), position, has_gauss, cached))


class NumpyHook(importlib.abc.MetaPathFinder):
    """Notice the first import of numpy, whenever it happens, and read its generator.

    ``np.random.get_state()`` is read right after the import finishes. With a record
    from an earlier run the state is set to it instead, at that same point. LDR never
    seeds numpy itself, so a run that does not ask for a restore behaves as it would
    without LDR.
    """

    def __init__(self, on_import, restore: str | None = None):
        self.on_import = on_import
        self.restore = restore
        self.fired = False

    def find_spec(self, name, path=None, target=None):
        if name != "numpy" or self.fired:
            return None
        self.fired = True  # one notice only
        spec = None
        for finder in sys.meta_path:
            if finder is not self and hasattr(finder, "find_spec"):
                spec = finder.find_spec(name, path, target)
                if spec is not None:
                    break
        if spec is None:
            return None
        loader = spec.loader
        if loader is None or not hasattr(loader, "exec_module"):
            return spec
        original = loader.exec_module
        hook = self

        def exec_module(module):
            original(module)
            hook._after_import(module)

        loader.exec_module = exec_module
        return spec

    def _after_import(self, numpy) -> None:
        try:
            if self.restore is not None:
                _restore_numpy(numpy, self.restore)
            self.on_import(_numpy_state(numpy))
        except Exception:  # a numpy without the legacy state API: nothing to record
            pass

    def remove(self) -> None:
        if self in sys.meta_path:
            sys.meta_path.remove(self)


def install_numpy_hook(on_import, restore: str | None = None) -> NumpyHook:
    """Install the hook; if numpy is already imported, read it now."""
    hook = NumpyHook(on_import, restore)
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        hook.fired = True
        hook._after_import(numpy)
    else:
        sys.meta_path.insert(0, hook)
    return hook


def randomly_seed(config):
    """The seed pytest-randomly uses, or None when that plugin is not active."""
    manager = getattr(config, "pluginmanager", None)
    if manager is None or not (manager.hasplugin("randomly") or manager.hasplugin("pytest_randomly")):
        return None
    try:
        return config.getoption("randomly_seed")
    except (ValueError, AttributeError):
        return None


def randomly_arguments(seed) -> list[str]:
    """Arguments that make a rerun use the recorded pytest-randomly seed."""
    return ["-p", "randomly", f"--randomly-seed={seed}"] if seed is not None else []
