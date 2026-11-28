"""Apply the independent v2 coverage oracle to fingerprint-test walk inputs."""
import pytest
from collections import Counter

CHILD_TOTAL=Counter()


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
    if fp._native_reader is not None or binary.exists():
        from test_native_v2 import reader
        native_engine=reader()
    child_reports=None
    if native_engine is not None:
        import os
        import json
        from ldr import processes
        child_reports=request.getfixturevalue('tmp_path_factory').mktemp('native-child-parity')
        hook=child_reports/'hook';hook.mkdir()
        helper=str(Path(__file__).parent)
        (hook/'sitecustomize.py').write_text(
            f"import sys\nsys.path.append({helper!r})\n"
            f"from native_child_oracle import install\ninstall({str(child_reports)!r})\n")
        original_options=processes._options
        def options(kwargs,skip=()):
            kwargs=dict(kwargs)
            env=dict(kwargs['env'] if kwargs.get('env') is not None else os.environ)
            env['PYTHONPATH']=str(hook)+os.pathsep+env.get('PYTHONPATH','')
            kwargs['env']=env
            return original_options(kwargs,skip)
        monkeypatch.setattr(processes,'_options',options)
    failures = []
    def covered(walk, value, out):
        try:
            result = check(value, encode=original, walk=walk, partial_ok=walk.partial_ok)
            if native_engine is not None:
                normalized=fp.STATS.tmp_normalized
                try:
                    actual=native_engine.walk(value,budget=walk.budget,partial_ok=walk.partial_ok)
                finally:
                    fp.STATS.tmp_normalized=normalized
                status=result['error'].status if result['error'] else 'complete'
                if (actual['bytes']!=result['bytes'] or actual['visited']!=walk.visited or actual['status']!=status
                    or actual['partial']!=walk.partial or actual['immutable']!=walk.immutable):
                    raise AssertionError('native parity mismatch for '+fp.type_identity(type(value)))
        except Exception as exc:
            failures.append(exc)
            raise
        out.update(result['bytes'])
        if result['error'] is not None:
            raise result['error']
    monkeypatch.setattr(fp._Walk, 'encode', covered)
    if native_engine is not None:
        original_details=fp._fingerprint_details
        def details(value,budget,partial_ok,sketch=False):
            if fp._native_reader is not None:
                class Sink:
                    def update(self,data): pass
                normalized=fp.STATS.tmp_normalized
                try:
                    covered(fp._Walk(budget,partial_ok),value,Sink())
                except fp._Stop:
                    pass  # The original boundary maps this status/sketch below.
                finally:
                    fp.STATS.tmp_normalized=normalized
            return original_details(value,budget,partial_ok,sketch)
        monkeypatch.setattr(fp,'_fingerprint_details',details)
    yield
    if child_reports is not None:
        for path in child_reports.glob('*.json'):
            report=json.loads(path.read_text())
            CHILD_TOTAL['values']+=report['values'];CHILD_TOTAL['nodes']+=report['nodes']
            failures.extend(report['failures'])
    assert not failures, failures


def pytest_terminal_summary(terminalreporter):
    from signature_coverage import TOTAL
    if TOTAL:
        terminalreporter.write_line(f"Signature coverage: {TOTAL['values']} values, {TOTAL['positions']} positions, 0 missing")
    if CHILD_TOTAL:
        terminalreporter.write_line(f"Native subprocess parity: {CHILD_TOTAL['values']} values, {CHILD_TOTAL['nodes']} nodes, 0 mismatches")
