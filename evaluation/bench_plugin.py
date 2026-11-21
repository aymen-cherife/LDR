"""pytest plugin for the attrs fixture: no hypothesis deadline.

attrs' own conftest sets a 400 ms deadline per example. Under tracing, or on a loaded
machine, an example can pass that without any change in behaviour, and the run then
fails. Loaded last, so it wins over the conftest; the seed stays pinned by the caller.
"""

import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    try:
        from hypothesis import settings
    except ImportError:
        return
    settings.register_profile("bench", settings.default, deadline=None)
    settings.load_profile("bench")
