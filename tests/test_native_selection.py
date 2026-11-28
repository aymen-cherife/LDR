"""Runtime gates and disclosure must agree with the reader that actually runs."""
import sys
import pytest
from ldr import fingerprint as fp
from ldr import native_bridge as bridge


def test_load_time_self_check_accepts_built_reader():
    from test_native_v2 import native
    engine,reason=bridge.select_reader(native())
    assert engine is not None,reason
    assert reason is None


def test_gate_rejects_other_versions_and_free_threaded(monkeypatch):
    from test_native_v2 import native
    module=native()
    monkeypatch.setattr(bridge,'python_build',lambda:'free-threaded')
    assert bridge.select_reader(module)[0] is None
    assert 'free-threaded' in bridge.select_reader(module)[1]
    monkeypatch.setattr(bridge,'python_build',lambda:'normal')
    monkeypatch.setattr(sys,'version_info',(3,14,0))
    assert bridge.select_reader(module)[0] is None
    assert '3.12 or 3.13' in bridge.select_reader(module)[1]


def test_self_check_mismatch_disables_reader(monkeypatch):
    from test_native_v2 import native
    original=bridge.Reader.capture
    def corrupt(self,*args,**kwargs):
        result=original(self,*args,**kwargs);result['visited']+=1;return result
    monkeypatch.setattr(bridge.Reader,'capture',corrupt)
    engine,reason=bridge.select_reader(native())
    assert engine is None and 'self-check' in reason and 'visited' in reason


def test_missing_watcher_disables_reader(monkeypatch):
    from test_native_v2 import native
    module=native()
    monkeypatch.setattr(module,'watcher_count',lambda:0,raising=False)
    engine,reason=bridge.select_reader(module)
    assert engine is None and 'watcher' in reason


def test_disclosure_has_exactly_three_fields():
    facts=bridge.reader_metadata()
    assert set(facts)=={'signature_reader','reader_versions','python_build'}
    assert facts['signature_reader']['name'] in {'native','python'}
    assert facts['reader_versions']['ldr']
    assert facts['python_build'] in {'normal','free-threaded'}
    if fp._native_reader is None:
        assert facts['signature_reader']['reason']


def test_native_panic_is_a_python_exception():
    from test_native_v2 import native
    module=native()
    with pytest.raises(BaseException,match='injected native panic'):
        module._test_panic()
    assert module.numeric_walk(1)[2]==1


def test_selected_reader_is_safe_from_another_thread():
    import threading
    from test_native_v2 import native
    engine,reason=bridge.select_reader(native())
    assert engine is not None,reason
    results=[]
    thread=threading.Thread(target=lambda: results.append(engine.capture([1,2])))
    thread.start();thread.join()
    assert results[0]['visited']==3


def test_result_and_machine_disclose_same_three_fields(tmp_path):
    import json
    from ldr import cli,machine
    expected=bridge.reader_metadata()
    cli._write_result('',{'status':{}},str(tmp_path))
    result=json.loads((tmp_path/'last.json').read_text())
    assert {key:result[key] for key in expected}==expected
    report=machine.check(str(tmp_path),keep=2,sample=False,python_start=False,fix=False)
    assert {key:report.facts[key] for key in expected}==expected
    text=report.summary()
    assert expected['signature_reader']['name'] in text
    assert expected['reader_versions']['ldr'] in text
    assert expected['python_build'] in text


def test_self_check_exercises_inline_dicts_before_python_materializes_them(monkeypatch):
    import gc
    from test_native_v2 import native
    original=bridge.Reader.capture
    checked=[]
    def capture(self,value,*args,**kwargs):
        if type(value).__name__ in ('DictValue','Both'):
            # 3.13 eagerly allocates the mixed-slot class's dictionary; the
            # plain managed-dict instance must exercise the inline path first.
            if type(value).__name__=='DictValue' or sys.version_info[:2]==(3,12):
                assert not any(type(item) is dict for item in gc.get_referents(value))
            checked.append(type(value).__name__)
        return original(self,value,*args,**kwargs)
    monkeypatch.setattr(bridge.Reader,'capture',capture)
    engine,reason=bridge.select_reader(native())
    assert engine is not None,reason
    assert checked==['DictValue','Both']


def test_python_fallback_can_fingerprint_another_value(monkeypatch):
    from test_native_v2 import native
    monkeypatch.setattr(fp,'_native_reader',None)
    expected=fp._fingerprint_details([1],fp.DEFAULT_BUDGET,True)[0]
    engine,reason=bridge.select_reader(native())
    assert engine is not None,reason
    monkeypatch.setattr(fp,'_native_reader',engine)
    class Key:
        @property
        def __class__(self):
            self.nested=fp._fingerprint_details([1],fp.DEFAULT_BUDGET,True)[0]
            return Key
    key=Key()
    assert fp._fingerprint_details({key:1},fp.DEFAULT_BUDGET,True)[0] not in fp.STATUSES
    assert key.nested==expected


def test_self_check_panic_disables_native(monkeypatch):
    from test_native_v2 import native
    module=native()
    def panic(*args,**kwargs): module._test_panic()
    monkeypatch.setattr(bridge.Reader,'capture',panic)
    engine,reason=bridge.select_reader(module)
    assert engine is None and 'PanicException' in reason


@pytest.mark.parametrize('value,partial',[(iter([1]),False)])
def test_native_status_uses_the_python_status_constant(monkeypatch,value,partial):
    from test_native_v2 import native
    engine,reason=bridge.select_reader(native())
    assert engine is not None,reason
    monkeypatch.setattr(fp,'_native_reader',engine)
    token,_=fp._fingerprint_details(value,fp.DEFAULT_BUDGET,partial)
    assert token is fp.UNSUPPORTED
