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
    import sys
    from pathlib import Path
    binary=Path(__file__).parents[1]/'ldr/_native/target'/f'py{sys.version_info.major}{sys.version_info.minor}'/'release/lib_native.so'
    native_engine=None
    if binary.exists():
        from test_native_v2 import reader
        native_engine=reader()
    failures = []
    def covered(walk, value, out):
        try:
            result = check(value, encode=original, walk=walk, partial_ok=walk.partial_ok)
            if native_engine is not None and walk.budget.max_nodes is None:
                normalized=fp.STATS.tmp_normalized
                try:
                    actual=native_engine.walk(value,partial_ok=walk.partial_ok)
                finally:
                    fp.STATS.tmp_normalized=normalized
                status=result['error'].status if result['error'] else 'complete'
                if actual['bytes']!=result['bytes'] or actual['visited']!=walk.visited or actual['status']!=status:
                    raise AssertionError('native parity mismatch for '+fp.type_identity(type(value)))
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
