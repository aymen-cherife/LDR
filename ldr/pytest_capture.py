"""Run LDR's recorder in the processes that execute pytest tests."""

import re
import sqlite3

import pytest

from .call_stream import CallStream
from .sqlite_store import TraceStore
from .tracer import Recorder


def _test_key(root) -> str:
    """The node id at the front of a root's address: ``path::name`` up to the next ``/``."""
    match = re.match(r"(?:[^/:]+/)*[^/]*?::[^/]*", root.frame_id)
    return match.group(0) if match else root.frame_id


class RuntimeCapture:
    def __init__(self, recorder):
        self.recorder = recorder
        self.exception = None

    def begin_test(self, item, prefix="", partition=""):
        """Make the test's node id the root of its call addresses."""
        function = getattr(item, "function", None)
        code = getattr(function, "__code__", None)
        self.recorder.begin_test(f"{prefix}{item.nodeid}", code, partition)

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
        self.store = TraceStore(**settings["store"])
        self.name = config.workerinput["workerid"]
        self.recorder.call_stream = CallStream(self.store, self.name)
        self.prefix = f"{self.name}/" if settings["replicated"] else ""

    def pytest_runtest_protocol(self, item, nextitem):
        # The node id is stable however pytest assigns tests to workers. Only a
        # replicated run, where every worker runs every test, needs the worker name.
        self.begin_test(item, self.prefix, f"{self.prefix}{item.nodeid}/")
        if not self.recorder._active:
            self.recorder.start()

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
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
            "store": vars(self.store),
            "replicated": node.config.getoption("dist") == "each",
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
