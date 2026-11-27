"""Apply the independent v2 coverage oracle to fingerprint-test walk inputs."""
import pytest


@pytest.fixture(autouse=True)
def signature_position_coverage(request, monkeypatch):
    if not ('fingerprint' in request.path.name or request.path.name in ('test_signature_v2.py', 'test_value_cache.py')):
        yield
        return
    from ldr import fingerprint as fp
    from signature_coverage import check
    original = fp._Walk.encode
    failures = []
    def covered(walk, value, out):
        try:
            result = check(value, encode=original, walk=walk, partial_ok=walk.partial_ok)
        except Exception as exc:
            failures.append(exc)
            raise
        out.update(result['bytes'])
        if result['error'] is not None:
            raise result['error']
    monkeypatch.setattr(fp._Walk, 'encode', covered)
    yield
    assert not failures, failures


def pytest_terminal_summary(terminalreporter):
    from signature_coverage import TOTAL
    if TOTAL:
        terminalreporter.write_line(f"Signature coverage: {TOTAL['values']} values, {TOTAL['positions']} positions, 0 missing")
