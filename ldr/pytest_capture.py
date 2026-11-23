"""Run LDR's recorder in the processes that execute pytest tests."""

import re
import sqlite3

import pytest

from . import fingerprint, reproduce
from .call_stream import CallStream
from .previous import encode_node_id
from .sink import store_from_settings
from .tracer import Recorder


def _test_key(root) -> str:
    """The node id at the front of a root's address: ``path::name`` up to the next ``/``."""
    match = re.match(r"(?:[^/:]+/)*[^/]*?::[^/]*", root.frame_id)
    return match.group(0) if match else root.frame_id


FLUSH_TESTS = 20


class RuntimeCapture:
    # Set by the subclasses: where tests are recorded, and the state of `random` that a
    # rerun hands back, by node id. Neither is set in a plain run.
    store = None
    worker = "main"
    rerun_random: dict = {}

    def __init__(self, recorder):
        self.recorder = recorder
        self.exception = None
        self.position = 0
        self.started: list[tuple[int, str, str | None]] = []
        self._checked_randomly = False
        self.randomly = None

    def begin_test(self, item, prefix="", partition=""):
        """Make the test's node id the root of its call addresses."""
        function = getattr(item, "function", None)
        code = getattr(function, "__code__", None)
        self.recorder.begin_test(f"{prefix}{encode_node_id(item.nodeid)}", code, partition)
        self.note_test(item, prefix)

    def note_test(self, item, prefix=""):
        """Rules 7 and 8: the order tests start in, and the state of `random` at each start.

        pytest-randomly, when active, owns seeding: its seed is recorded and `random` is
        neither read nor restored here.
        """
        if not self._checked_randomly:
            self._checked_randomly = True
            self.randomly = reproduce.randomly_seed(item.config)
            if self.randomly is not None and self.store is not None:
                self._save(lambda: self.store.save_rng("pytest-randomly", str(self.randomly)))
        node = f"{prefix}{item.nodeid}"
        state = None
        if self.randomly is None:
            wanted = self.rerun_random.get(node)
            if wanted is not None:
                reproduce.restore_random(wanted)
            state = reproduce.capture_random()
        self.position += 1
        self.started.append((self.position, node, state))
        if len(self.started) >= FLUSH_TESTS:
            self.flush_tests()

    def flush_tests(self):
        if self.store is not None and self.started:
            rows, self.started = self.started, []
            self._save(lambda: self.store.save_tests(self.worker, rows))

    @staticmethod
    def _save(action):
        try:
            action()
        except (OSError, EOFError, sqlite3.Error):
            pass  # secondary evidence: never lose the call stream over it

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.flush_tests()

    def pytest_runtest_protocol(self, item, nextitem):
        self.begin_test(item)
        if not self.recorder._active:
            self.recorder.start()

    def pytest_runtest_makereport(self, item, call):
        if call.excinfo is not None:
            self.exception = call.excinfo.value
            self.recorder.runner_exception = self.exception


class WorkerCapture(RuntimeCapture):
    def __init__(self, config, settings):
        super().__init__(Recorder(**settings["recorder"]))
        self.config = config
        self.store = store_from_settings(settings["store"])
        self.store.full_tests = tuple(settings.get("full_tests") or ())
        fingerprint.configure(self.store.walk_ceiling)
        self.name = config.workerinput["workerid"]
        self.worker = self.name
        self.rerun_random = settings.get("rerun_random") or {}
        self.numpy_hook = reproduce.install_numpy_hook(
            lambda state: self._save(lambda: self.store.save_rng(f"numpy-at-import:{self.name}", state)),
            (settings.get("rerun_numpy") or {}).get(self.name),
        )
        self.recorder.call_stream = CallStream(self.store, self.name)
        self.prefix = f"{self.name}/" if settings["replicated"] else ""

    def pytest_runtest_protocol(self, item, nextitem):
        # The node id is stable however pytest assigns tests to workers. Only a
        # replicated run, where every worker runs every test, needs the worker name.
        self.begin_test(item, self.prefix, f"{self.prefix}{encode_node_id(item.nodeid)}/")
        if not self.recorder._active:
            self.recorder.start()

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.flush_tests()
        self.numpy_hook.remove()
        self.recorder.stop()
        self.recorder.finalize(
            success=exitstatus == 0, escaped_exception=self.exception
        )
        self.recorder.call_stream.finish()
        try:
            self.store.save_worker(
                self.name,
                self.recorder.roots,
                {
                    key: getattr(self.recorder, key)
                    for key in (
                        "n_nodes",
                        "dropped_nodes",
                        "sampled_nodes",
                        "truncated",
                        "inspection_matches",
                    )
                },
            )
        except (OSError, sqlite3.Error):
            self.config.workeroutput["ldr_capture_failed"] = True


class ControllerCapture(RuntimeCapture):
    def __init__(self, recorder, store, options):
        super().__init__(recorder)
        self.store = store
        self.options = options
        self.workers = {}

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node):
        name = node.workerinput["workerid"]
        self.workers[name] = "incomplete"
        if self.store is None or not node.gateway.spec.popen or node.gateway.spec.via:
            return
        node.workerinput["ldr_capture"] = {
            "recorder": self.options,
            "store": self.store.settings(),
            "replicated": node.config.getoption("dist") == "each",
            "rerun_random": self.rerun_random,
            "full_tests": list(getattr(self.store, "full_tests", ())),
        }

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        output = getattr(node, "workeroutput", {})
        if (
            error is None
            and "ldr_capture" in node.workerinput
            and not output.get("ldr_capture_failed")
        ):
            self.workers[node.workerinput["workerid"]] = "finished"

    def merge(self):
        for name, state in self.workers.items():
            if state != "finished":
                continue
            try:
                capture = self.store.load_worker(name)
            except (OSError, sqlite3.Error):
                capture = None
            if capture is None:
                self.workers[name] = "incomplete"
                continue
            roots, summary = capture
            self.recorder.roots.extend(roots)
            self.recorder.truncated |= summary.pop("truncated")
            for key, value in summary.items():
                setattr(self.recorder, key, getattr(self.recorder, key) + value)
        # Stable across worker assignment: group a test's roots, in recorded order.
        self.recorder.roots.sort(key=_test_key)
        return [name for name, state in self.workers.items() if state == "incomplete"]
