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
