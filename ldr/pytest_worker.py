"""Pytest plugin entry point loaded by xdist in each worker interpreter."""

from .pytest_capture import WorkerCapture
from .redact import register_argv_secrets
from .tracer import python_version_message


def pytest_configure(config):
    settings = getattr(config, "workerinput", {}).get("ldr_capture")
    if settings and python_version_message() is None:
        register_argv_secrets(list(config.invocation_params.args))
        config.pluginmanager.register(WorkerCapture(config, settings))
