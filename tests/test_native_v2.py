"""Parity tests for the standalone native walk core, before runtime integration."""
import hashlib
import importlib.util
import importlib
from pathlib import Path
import random
import sys
import pytest

from ldr import fingerprint as fp


def native():
    # Prefer the importable extension so runtime and tests share one watcher.
    try:
        module = importlib.import_module('ldr._native')
    except ModuleNotFoundError as error:
        if error.name != 'ldr._native':
            raise
        module = None
    if hasattr(module, 'ReaderCore'):
        return module
    path = Path(__file__).parents[1] / 'ldr/_native/target' / f'py{sys.version_info.major}{sys.version_info.minor}' / 'release/lib_native.so'
    if not path.exists():
        pytest.skip('native module not built')
    spec = importlib.util.spec_from_file_location('_native', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_numeric_container_bytes_and_counts():
    module = native()
    rng = random.Random(0)
    values = [None, True, False, -(2**63), 2**63-1, -0.0, 0.0, float('inf'), float('-inf'), float('nan')]
    values += [[rng.randint(-100000,100000), (rng.random(), None, True)] for _ in range(100)]
    values += [{1: [2, 3], 4: (5, False)}]
    for value in values:
        class Bytes:
            def __init__(self): self.data = bytearray()
            def update(self, data): self.data.extend(data)
        output = Bytes()
        walk = fp._Walk(fp.DEFAULT_BUDGET)
        walk.encode(value, output)
        encoded, digest, visited = module.numeric_walk(value)
        assert encoded == bytes(output.data)
        assert digest == hashlib.blake2b(output.data, digest_size=16).hexdigest()
        assert visited == walk.visited


def test_cycles_and_100000_levels():
    module = native()
    cycle = []
    cycle.append(cycle)
    assert module.numeric_walk(cycle)[2] == 2
    deep = 1
    for _ in range(100000): deep = [deep]
    encoded, digest, visited = module.numeric_walk(deep)
    assert visited == 100001
    assert encoded == (b'l' + (1).to_bytes(8,'little')) * 100000 + b'i' + (1).to_bytes(8,'little',signed=True)


def test_unicode_buffer_preserves_utf8_and_surrogates():
    module = native()
    from test_value_cache import corpus
    values = ['', 'plain', 'é', '日本', '\U0001f600', '\ud800', '\udfff', 'a\ud800é\U0001f600\udfff']
    values.extend(corpus())
    values.extend(chr(code) for code in range(0xD800, 0xE000))
    for value in values:
        assert module.unicode_payload(value) == value.encode('utf8', 'surrogatepass')


def reader():
    from ldr.native_bridge import Reader
    return Reader(native())


def python_wire(value, partial_ok=True):
    class Sink:
        def __init__(self): self.data=bytearray()
        def update(self,data): self.data.extend(data)
    sink=Sink(); walk=fp._Walk(fp.DEFAULT_BUDGET,partial_ok)
    status='complete'
    try: walk.encode(value,sink)
    except fp._Stop as exc: status=exc.status
    return bytes(sink.data),walk.visited,status


def test_reader_strings_and_redaction_policy():
    from test_value_cache import values
    from ldr import redact
    engine=reader()
    for value in values():
        expected,count,status=python_wire(value)
        result=engine.walk(value)
        assert (result['bytes'],result['visited'],result['status'])==(expected,count,status)
    secret='native-policy-change-632741'
    try:
        redact.load_environment({'API_KEY':secret})
        assert engine.walk(secret)['status']==fp.REDACTED
        redact.load_environment({})
        assert engine.walk(secret)['status']=='complete'
    finally: redact.load_environment()


def test_reader_layout_watchers_and_rich():
    from rich.theme import ThemeStack,Theme
    from rich.style import Style
    from rich.text import Text
    engine=reader()
    class Base:
        __slots__=('base',)
    class Item(Base):
        __slots__=('slot','missing','__dict__')
    obj=Item(); obj.base=1; obj.slot='é\ud800'; obj.extra=[2,3]
    values=[obj,ThemeStack(Theme({'x':Style(color='red')})),Theme({}),Style(bold=True),Text('text')]
    for value in values:
        expected,count,status=python_wire(value)
        actual=engine.walk(value)
        assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)
    Base.base=property(lambda self: (_ for _ in ()).throw(AssertionError('property ran')))
    expected,count,status=python_wire(obj)
    actual=engine.walk(obj)
    assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)
    assert engine.stats()['watchers']==1


def test_reader_cache_lifetime_class_reassignment_and_temp_paths():
    import weakref,gc,tempfile,os
    engine=reader()
    class First:
        __slots__=('a',)
    class Second:
        __slots__=('a',)
    value=First();value.a=3
    engine.walk(value);value.__class__=Second
    assert engine.walk(value)['bytes']==python_wire(value)[0]
    def populate():
        class Temporary: pass
        instance=Temporary();instance.x='x'
        engine.walk(instance)
        return weakref.ref(Temporary)
    reference=populate();gc.collect();assert reference() is None
    for separator in [' ', '\u001c', '\u0085', '\u2003', '\u3000', "'", '"', '']:
        value='wrote '+os.path.join(tempfile.gettempdir(),'tmpnative','é\ud800')+separator+'done'
        assert engine.walk(value)['bytes']==python_wire(value)[0]


def test_reader_nested_fallback_cycles_and_secret_runs():
    import enum,datetime,functools,threading
    engine=reader()
    class Mode(enum.Enum): A=1
    class Hook:
        def __getattr__(self,name): raise AssertionError('hook called')
    root=[];child=Hook();child.root=root;root.append(child)
    values=[root,Mode.A,{1,frozenset({2,3})},[{'x':Mode.A},datetime.date(2026,1,1)],
            functools.partial(pow,2,exp=3),threading.Lock(),['plain']*39+['ghp_'+'a'*36]]
    for value in values:
        expected,count,status=python_wire(value)
        actual=engine.walk(value)
        if (actual['bytes'],actual['visited'],actual['status'])!=(expected,count,status):
            raise AssertionError('native parity failed for '+fp.type_identity(type(value)))


def test_reader_does_not_materialize_managed_dict_and_handles_shadowing():
    import gc
    engine=reader()
    class Plain: pass
    value=Plain();value.x=1
    before=any(type(item) is dict for item in gc.get_referents(value))
    engine.walk(value)
    after=any(type(item) is dict for item in gc.get_referents(value))
    assert before==after
    class Base:
        __slots__=('x',)
    class Child(Base):
        __slots__=()
        @property
        def x(self): raise AssertionError('property ran')
    value=Child();Base.__dict__['x'].__set__(value,7)
    expected,count,status=python_wire(value)
    actual=engine.walk(value)
    assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)
    assert actual['delegated']>=1


def test_reader_in_place_slot_declaration_edits_and_subclass_keys():
    engine=reader()
    class Item:
        __slots__=['__hidden']
    value=Item();value._Item__hidden=3
    engine.walk(value)
    Item.__slots__[0]='_Item__hidden'
    expected,count,status=python_wire(value)
    actual=engine.walk(value)
    assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)
    class Name(str): pass
    value={Name('api_key'):'synthetic-secret'}
    expected,count,status=python_wire(value)
    actual=engine.walk(value)
    assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)


def test_reader_unknown_key_policy_and_delegated_normalization():
    engine=reader()
    class Key:
        @property
        def __class__(self): return str
    value={Key():1}
    actual=engine.walk(value)
    assert actual['status']==fp.FAILED
    assert actual['delegated']==1
    assert engine.walk('label=/tmp/tmpnative/é')['normalized']==1


def test_reader_theme_color_dependencies_are_native():
    from rich.color import Color
    from rich.theme import Theme,ThemeStack
    engine=reader()
    color=Color.parse('red')
    result=engine.walk(color)
    assert result['delegated']==0
    assert result['bytes']==python_wire(color)[0]
    # The builtin dict.get bound method remains a separate delegated leaf.
    result=engine.walk(ThemeStack(Theme({'x':__import__('rich.style',fromlist=['Style']).Style(color='red')},inherit=False)))
    assert result['delegated']==1


def test_reader_redaction_python_ascii_whitespace():
    engine=reader()
    for code in range(0x1c,0x20):
        for value in ['Bearer'+chr(code)+'abcdefghijklmnop', 'https://user:'+chr(code)+'password@example.test/', '?token='+chr(code)+'value']:
            expected,count,status=python_wire(value)
            actual=engine.walk(value)
            if (actual['bytes'],actual['visited'],actual['status'])!=(expected,count,status):
                raise AssertionError('Python whitespace policy mismatch')


def test_reader_shadowed_instance_dict_keeps_python_handling():
    engine=reader()
    class Hidden:
        @property
        def __dict__(self): raise AssertionError('property ran')
    value=Hidden();value.hidden=7
    expected,count,status=python_wire(value)
    actual=engine.walk(value)
    assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)
    assert actual['delegated']==1


def test_reader_bulk_redaction_normalization_counts_and_fast_mode(monkeypatch):
    engine=reader()
    value=['/tmp/tmpnative/file']*40+['ghp_'+'a'*36]
    before=fp.STATS.tmp_normalized
    expected,count,status=python_wire(value)
    normalized=fp.STATS.tmp_normalized-before
    actual=engine.walk(value)
    assert (actual['bytes'],actual['visited'],actual['status'],actual['normalized'])==(expected,count,status,normalized)
    monkeypatch.setattr(fp,'FAST_PATH_MIN',10**12)
    expected,count,status=python_wire(value)
    actual=engine.walk(value)
    assert (actual['bytes'],actual['visited'],actual['status'])==(expected,count,status)

@pytest.mark.parametrize('ceiling',[31,32,33,None])
@pytest.mark.parametrize('value',[[0]*32, ['x']*32, [0,'x']*16, {'a':list(range(40))}])
def test_recording_contract_ceiling(value,ceiling):
    import importlib.util
    spec=importlib.util.spec_from_file_location('signature_bench',Path(__file__).parents[1]/'evaluation/signature_bench.py')
    bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)
    budget=fp.Budget(ceiling)
    expected=bench.python_wire(value,budget,True)
    result=reader().capture(value,budget=budget,partial_ok=True)
    assert 'bytes' not in result
    for key in ('visited','status','partial','immutable','normalized'):
        assert result[key]==expected[key],key
    assert result['digest']==hashlib.blake2b(expected['bytes'],digest_size=16).hexdigest()
    assert result['over_ceiling']==(ceiling is not None and result['visited']>ceiling)


def test_fallback_protects_pending_values_and_detects_dict_resize():
    import gc
    engine=reader()
    class Cached: pass
    cached=[Cached]
    engine.walk(Cached())
    import weakref
    reference=weakref.ref(Cached)
    del Cached
    class Key:
        @property
        def __class__(self):
            cached.clear();gc.collect()
            mapping['new']=[]
            return Key
    key=Key();mapping={key:[], 'last':[]}
    result=engine.capture(mapping)
    assert result['status']==fp.FAILED
    assert not cached and reference() is None
    assert engine.capture([1,2])['status']=='complete'

@pytest.mark.parametrize('partial_ok',[False,True])
def test_recording_contract_preserves_partial_token_and_stats(monkeypatch,partial_ok):
    import threading
    engine=reader()
    value=[threading.Lock(),{'a':[1,2]}]
    monkeypatch.setattr(fp,'_native_reader',None,raising=False)
    before=fp._fingerprint_details(value,fp.Budget(20000),partial_ok)
    expected=(fp.STATS.walked,fp.STATS.walked_type,fp.STATS.last_visited)
    calls=[]
    capture=engine.capture
    def once(*args,**kwargs):
        calls.append(args[0]);return capture(*args,**kwargs)
    monkeypatch.setattr(engine,'capture',once)
    monkeypatch.setattr(fp,'_native_reader',engine)
    actual=fp._fingerprint_details(value,fp.Budget(20000),partial_ok)
    assert len(calls)==1 and calls[0] is value
    assert actual==before
    assert (fp.STATS.walked,fp.STATS.walked_type,fp.STATS.last_visited)==expected
    assert actual[0].endswith('+')==partial_ok


def test_recording_contract_preserves_sketch_and_size_count(monkeypatch):
    value=list(range(20001))
    monkeypatch.setattr(fp,'_native_reader',None,raising=False)
    expected=fp._fingerprint_details(value,fp.Budget(20000),True,sketch=True)
    count=fp.STATS.walked
    monkeypatch.setattr(fp,'_native_reader',reader())
    assert fp._fingerprint_details(value,fp.Budget(20000),True,sketch=True)==expected
    assert fp.STATS.walked==count==20002
