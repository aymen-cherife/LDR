"""Gate 1: use the Python encoder as the oracle, including failed walks."""
import hashlib
import sys
import pytest
from ldr import fingerprint as fp
from test_native_v2 import reader


def parity(engine,value,ceiling=None,partial_ok=True):
    class Wire:
        def __init__(self): self.data=bytearray()
        def update(self,data): self.data.extend(data)
    wire=Wire();walk=fp._Walk(fp.Budget(ceiling),partial_ok)
    before=fp.STATS.tmp_normalized
    status='complete'
    try: walk.encode(value,wire)
    except fp._Stop as error: status=error.status
    except RecursionError: status=fp.UNKNOWN
    except Exception: status=fp.FAILED
    normalized=fp.STATS.tmp_normalized-before
    fp.STATS.tmp_normalized=before
    result=engine.walk(value,fp.Budget(ceiling),partial_ok)
    expected={'bytes':wire.data,'digest':hashlib.blake2b(wire.data,digest_size=16).hexdigest(),
              'visited':walk.visited,'status':status,'partial':walk.partial,
              'immutable':walk.immutable,'normalized':normalized,
              'over_ceiling':ceiling is not None and walk.visited>ceiling}
    for key,value in expected.items(): assert result[key]==value,key
    assert sum(result["delegated_by_type"].values())==result["delegated"]
    return result


def test_hypothesis_all_encoded_types():
    engine=reader()  # CI without the build skips with the prescribed reason.
    from hypothesis import given,settings,strategies as st
    import collections,datetime,decimal,enum,functools,struct,tempfile
    class Slots:
        __slots__=('set','unset','__dict__')
    class Dict: pass
    class Int(int): pass
    class List(list): pass
    class Kind(enum.Enum): ITEM=1
    Point=collections.namedtuple('Point','x y')
    leaves=st.one_of(st.none(),st.booleans(),st.integers(),st.floats(),
                     st.text(),st.binary(),st.sampled_from(['\ud800','日本','é','-0.0']))
    values=st.recursive(leaves,lambda child: st.one_of(st.lists(child),st.lists(child).map(tuple),
                       st.dictionaries(st.text(),child),st.sets(st.integers()),st.frozensets(st.integers())))
    @settings(deadline=None)
    @given(values,st.sampled_from([None,0,1,32,20000]),st.booleans())
    def check(value,ceiling,partial):
        slot=Slots();slot.set=value;slot.extra=1
        obj=Dict();obj.field=value
        cycle=[value];cycle.append(cycle)
        cases=[value,slot,obj,cycle,Int(3),List([value]),Kind.ITEM,Point(value,2),
               ValueError(value),datetime.datetime(2026,1,2),datetime.date(2026,1,2),
               datetime.time(1,2),datetime.timedelta(days=3),decimal.Decimal('1.2'),complex(-0.,float('nan')),
               -0.0,struct.unpack('<d',bytes.fromhex('010000000000f87f'))[0],2**200,-2**200,
               bytearray(b'abc'),range(2),Ellipsis,NotImplemented,functools.partial(pow,2),pow,
               'ghp_'+'a'*36,tempfile.gettempdir()+'/case',{'api_key':value}]
        for case in cases: parity(engine,case,ceiling,partial)
    check()


@pytest.mark.parametrize('items',[19998,19999,20000])
def test_twenty_thousand_node_boundary(items):
    value=[None if i%2 else i for i in range(items)]
    result=parity(reader(),value,20000)
    assert result['visited']==items+1
    assert result['over_ceiling']==(items+1>20000)


def test_deep_walk_and_fallback_shared_cycle():
    import functools
    engine=reader()
    value=0
    for _ in range(100000): value=[value]
    assert parity(engine,value)['visited']==100001
    root=[];partial=functools.partial(list,root);root.append(partial)
    parity(engine,root)


def test_freed_class_address_cannot_hit_stale_layout():
    import gc,weakref
    engine=reader()
    retired=set()
    while True:
        cls=type('Temporary',(),{'__slots__':('a',)})
        instance=cls();instance.a=17
        address=id(cls)
        result=parity(engine,instance)
        if address in retired: break
        retired.add(address)
        reference=weakref.ref(cls)
        del instance,cls
        gc.collect()
        assert reference() is None
    instance.a='new state'
    parity(engine,instance)


@pytest.mark.parametrize('seed',['0','1','random'])
def test_signatures_independent_of_hash_seed(seed,tmp_path):
    import os,json
    from ldr import processes
    engine=reader()
    from pathlib import Path
    script='''from ldr import fingerprint as fp
from ldr.native_bridge import select_reader
import json
engine,reason=select_reader()
assert engine is not None,reason
values=[set(['a','b','é']),frozenset([1,2,'é']),dict([('b',[1,2]),('a',{'x','y'})])]
print(json.dumps([engine.capture(value)['digest'] for value in values]))
'''
    env=dict(os.environ,PYTHONHASHSEED=seed,PYTHONPATH=str(Path(__file__).parents[1]))
    result=processes.check_output([sys.executable,'-c',script],env=env,text=True)
    values=[set(['a','b','é']),frozenset([1,2,'é']),dict([('b',[1,2]),('a',{'x','y'})])]
    assert json.loads(result)==[parity(engine,value)['digest'] for value in values]


def test_blueprint_string_decisions_stay_with_python():
    engine=reader()
    for value in ['é','\ud800','/tmp/case','?key=ordinary','ghp_'+'a'*36]:
        result=parity(engine,value)
        assert result['delegated']==1
        assert sum(result['delegated_by_type'].values())==1
    assert parity(engine,'plain ASCII')['delegated']==0


@pytest.mark.parametrize('value',[[0]*40,[1.0]*40,[True]*40])
def test_existing_numeric_coverage_stays_native_in_bulk(value):
    assert parity(reader(),value)['delegated']==0


def test_fallback_detects_a_resized_outer_set():
    engine=reader()
    class Entry: pass
    class Key:
        @property
        def __class__(self):
            outer.add(2)
            return Key
    entry=Entry();entry.mapping={Key():1}
    outer={entry}
    result=engine.walk(outer)
    assert result['status']==fp.FAILED


def test_cached_class_can_be_collected_during_a_native_walk():
    import gc,weakref
    engine=reader()
    gc.collect()
    class Cached: pass
    holder=[Cached]
    engine.walk(Cached())
    reference=weakref.ref(Cached)
    del Cached
    value=type('Fresh',(),{})()
    # Bind before scheduling collection: the callback must run in Rust's call,
    # not while resolving a Python bound method or preparing redaction inputs.
    capture=engine.core.capture
    dropped=[]
    def collection(phase,info):
        if phase=='start' and holder:
            dropped.append(True)
            holder.clear()
    enabled=gc.isenabled();schedule=gc.get_threshold()
    gc.enable();gc.callbacks.append(collection)
    try:
        # Force the next GC allocation (the cold layout's weak reference) to
        # collect. This changes test scheduling, never a recording node limit.
        gc.set_threshold(1,0,0)
        result=capture(value,True)
        assert result['status']=='complete'
        assert dropped and reference() is None
    finally:
        gc.set_threshold(*schedule)
        gc.callbacks.remove(collection)
        if not enabled: gc.disable()


@pytest.mark.parametrize('value',[[2**200]*40,['é']*40])
def test_bulk_fallback_returns_each_leaf_to_the_native_container(value):
    result=parity(reader(),value)
    assert result['delegated']==len(value)
    assert list not in result['delegated_by_type']


def test_non_ascii_secret_still_uses_pythons_policy():
    from ldr import redact
    secret='unicode-秘密-632741'
    try:
        redact.load_environment({'API_KEY':secret})
        assert parity(reader(),secret)['delegated']==1
    finally: redact.load_environment()


def test_bulk_fallback_keeps_the_container_alive_and_rereads_lengths(monkeypatch):
    original=fp.normalize_temp_paths
    def make():
        child=['é']*40
        root=[child]
        def normalize(value):
            if root:
                root[0].pop()
                root.clear()
            return original(value)
        return root,normalize
    class Wire:
        def __init__(self): self.data=bytearray()
        def update(self,data): self.data.extend(data)
    value,normalize=make();monkeypatch.setattr(fp,'normalize_temp_paths',normalize)
    walk=fp._Walk(fp.DEFAULT_BUDGET,True);wire=Wire();walk.encode(value,wire)
    value,normalize=make();monkeypatch.setattr(fp,'normalize_temp_paths',normalize)
    actual=reader().walk(value)
    assert actual['bytes']==wire.data
    assert actual['visited']==walk.visited==41
    assert actual['status']=='complete'
