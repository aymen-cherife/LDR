import importlib.util
from pathlib import Path


def module():
    spec=importlib.util.spec_from_file_location('signature_bench',Path(__file__).parents[1]/'evaluation'/'signature_bench.py')
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_samples_keep_references_and_preserve_every_walk():
    from ldr import fingerprint as fp
    bench=module()
    samples=bench.Samples()
    value=[1]
    samples.capture(value,fp.Budget(),True)
    samples.capture(value,fp.Budget(),True)
    value.append(2)
    assert len(samples.values)==2
    assert samples.values[0][0] is value
    result=samples.measure()
    assert result['values']==2 and result['nodes']==6
    assert result['seconds']>0 and result['ns_per_node']>0
    assert len(result['top_types'])==1


def test_test_result_differences_include_exact_node_and_phase():
    bench=module()
    normal={'test_a::one:call':'passed','test_a::two:call':'passed'}
    kept={'test_a::one:call':'failed','test_a::two:call':'passed'}
    assert bench.result_differences(normal,kept)==[
        {'position':'test_a::one:call','normal':'passed','kept':'failed'}]


def test_compare_native_on_identical_retained_values():
    import sys
    sys.path.insert(0,str(Path(__file__).parent))
    from test_native_v2 import reader
    from ldr import fingerprint as fp
    bench=module();samples=bench.Samples();value=[1,2]
    samples.capture(value,fp.Budget(),True)
    result=samples.compare(reader())
    assert result['parity_values']==1
    assert result['native']['nodes']==3
    assert result['native']['delegated_nodes']==0
    assert result['native']['native_share']==1


def test_compare_rejects_different_bytes():
    import pytest
    import sys
    sys.path.insert(0,str(Path(__file__).parent))
    from test_native_v2 import reader
    from ldr import fingerprint as fp
    actual=reader()
    class Broken:
        def walk(self,*args,**kwargs):
            result=actual.walk(*args,**kwargs);result['bytes']+=b'?';return result
    samples=module().Samples();samples.capture([1],fp.Budget(),True)
    with pytest.raises(AssertionError): samples.compare(Broken())


def test_peak_memory_on_windows_without_resource(monkeypatch):
    import builtins
    import ctypes
    import types
    bench = module()
    original_import = builtins.__import__
    def without_resource(name, *args, **kwargs):
        if name == "resource":
            raise ModuleNotFoundError("No module named 'resource'")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_resource)
    monkeypatch.setattr(bench.os, "name", "nt")
    def current():
        return 7
    def memory(handle, counters, size):
        assert handle == 7
        counters._obj.PeakWorkingSetSize = 123456
        return 1
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: (
        types.SimpleNamespace(GetCurrentProcess=current) if name == "kernel32"
        else types.SimpleNamespace(GetProcessMemoryInfo=memory)), raising=False)
    assert bench.peak_memory() == 123456


def test_native_measure_honors_the_same_ceiling():
    import sys
    sys.path.insert(0,str(Path(__file__).parent))
    from test_native_v2 import reader
    from ldr import fingerprint as fp
    bench=module();samples=bench.Samples()
    samples.capture([0,'x']*100,fp.Budget(32),True)
    result=samples.compare(reader())
    assert result['native']['nodes']==result['python']['nodes']==33
    assert result['native']['statuses']==result['python']['statuses']
    assert 'delegated_by_type' in result['native']


def test_retention_captures_native_boundary_without_a_python_walk(tmp_path,monkeypatch):
    import pytest
    import json
    from ldr import fingerprint as fp
    bench=module()
    class Native:
        def capture(self,value,budget,partial_ok):
            return {'digest':'a'*32,'visited':3,'partial':False,'immutable':False,
                    'normalized':0,'status':'complete'}
    monkeypatch.setattr(fp,'_native_reader',Native())
    value=[1,2]
    monkeypatch.setattr(pytest,'main',lambda **kw: (fp._fingerprint_details(value,fp.Budget(),True) and 0))
    seen=[]
    def compare(self,reader):
        seen.extend(self.values)
        return {'python':{},'native':{},'parity_values':len(self.values)}
    monkeypatch.setattr(bench.Samples,'compare',compare)
    monkeypatch.setattr(bench,'native_reader',lambda:None)
    destination=tmp_path/'results.json'
    bench.install(destination,True)
    assert pytest.main()==0
    assert seen[0][0] is value and len(seen)==1
    assert json.loads(destination.read_text())['parity_values']==1


def test_benchmark_disables_the_existing_timeout(tmp_path,monkeypatch):
    import sys,types,pytest
    bench=module()
    traced=[sys.executable,'-m','ldr','pytest','tests/a.py','--']
    fake=types.SimpleNamespace(build=lambda *args:(tmp_path,None,traced),
        environment=lambda *args:{'PYTHONPATH':str(tmp_path)},clean=lambda *args:None)
    monkeypatch.setitem(sys.modules,'overhead',fake)
    class Observed(Exception): pass
    def run(command,**kwargs):
        assert '--timeout=0' in command[:command.index('--')]
        raise Observed
    monkeypatch.setattr(bench.processes,'run',run)
    with pytest.raises(Observed): bench.main()
