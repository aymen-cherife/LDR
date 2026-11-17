"""README claims must hold on the platform they are made for."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))


def test_readme_does_not_promise_owner_only_files_everywhere():
    readme = open(os.path.join(os.path.dirname(HERE), "README.md"), encoding="utf-8").read()
    assert "owner-only" in readme  # the claim stays, but only where it is true
    assert "POSIX" in readme.split("owner-only")[1][:200] or "POSIX" in readme.split("owner-only")[0][-200:]

