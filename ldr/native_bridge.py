"""Prototype boundary for nodes the native reader cannot yet encode exactly."""
import re
import functools,decimal,datetime,types,abc,enum
SPECIAL_TYPES=(functools.partial,types.MethodType,types.FunctionType,types.BuiltinFunctionType,decimal.Decimal,datetime.date,datetime.time,datetime.datetime,datetime.timedelta,type,abc.ABCMeta,range,type(Ellipsis),type(NotImplemented))
from . import redact
ENUM_META=enum.EnumType


class Sink:
    def __init__(self): self.data=bytearray()
    def update(self,data): self.data.extend(data)
    def take(self):
        result=bytes(self.data);self.data.clear();return result


class Fallback:
    def __init__(self):
        global fp
        from . import fingerprint as fp
        self.walk=fp._Walk(fp.DEFAULT_BUDGET,True)
        self.sink=Sink()
    def secret_key(self,value):
        return fp.is_secret_name(value)
    def secret_text(self,value):
        return fp._looks_secret(value)
    def start(self,value,active,visited,partial_ok,max_nodes=None):
        self.walk.budget=fp.Budget(max_nodes)
        self.normalized_before=fp.STATS.tmp_normalized
        self.walk.active=active;self.walk.visited=visited-1;self.walk.partial_ok=partial_ok
        self.walk.partial=False;self.walk.immutable=True
        try:
            iterator=self.walk._encode(value,self.sink)
            return self.advance(iterator,active,self.walk.visited) if iterator is not None else self.result(None,None,'complete')
        except fp._Stop as exc: return self.result(None,None,exc.status)
        except RecursionError: return self.result(None,None,fp.UNKNOWN)
        except Exception: return self.result(None,None,fp.FAILED)
    def advance(self,iterator,active,visited):
        self.normalized_before=fp.STATS.tmp_normalized
        self.walk.active=active;self.walk.visited=visited
        try:
            child=next(iterator)
            value,target=child
            assert target is self.sink, 'fallback yielded an unsupported hash target'
            return self.result(iterator,value,'child')
        except StopIteration: return self.result(None,None,'complete')
        except fp._Stop as exc: return self.result(None,None,exc.status)
        except RecursionError: return self.result(None,None,fp.UNKNOWN)
        except Exception: return self.result(None,None,fp.FAILED)
    def result(self,iterator,child,status):
        return (self.sink.take(),iterator,child,status,self.walk.visited,self.walk.active,self.walk.partial,self.walk.immutable,fp.STATS.tmp_normalized-self.normalized_before)


DICT_GETTER=Fallback.__dict__['__dict__']


class Reader:
    def __init__(self,module):
        global fp
        from . import fingerprint as fp
        self.module=module;self.token=None;self.core=None;self.fast_min=None
    def refresh(self):
        token=redact.text_policy()
        if self.core is None or not redact.same_text_policy(self.token) or self.fast_min!=fp.FAST_PATH_MIN:
            patterns=[('(?i)' if pattern.flags & re.IGNORECASE and not pattern.pattern.startswith('(?i)') else '')+pattern.pattern for _,pattern,_ in redact._SHAPES]
            self.core=self.module.ReaderCore(fp._TMP_PREFIXES,
                [s.encode('utf8','surrogatepass') for s,_ in redact._environment[0]],
                [(s.encode('utf8','surrogatepass'),len(s)>=8) for s in redact._argv_secrets[1]],patterns,Fallback(),SPECIAL_TYPES,ENUM_META,DICT_GETTER,fp.FAST_PATH_MIN)
            self.token=token
            self.fast_min=fp.FAST_PATH_MIN
    def walk(self,value,budget=None,partial_ok=True):
        self.refresh()
        normalized=fp.STATS.tmp_normalized
        try:
            return self.core.walk(value,partial_ok,(budget or fp.DEFAULT_BUDGET).max_nodes)
        finally:
            fp.STATS.tmp_normalized=normalized
    def capture(self,value,budget=None,partial_ok=True):
        self.refresh()
        normalized=fp.STATS.tmp_normalized
        try:
            return self.core.capture(value,partial_ok,(budget or fp.DEFAULT_BUDGET).max_nodes)
        finally:
            fp.STATS.tmp_normalized=normalized
    def stats(self):
        return self.core.stats()


def python_build():
    import sysconfig
    return 'free-threaded' if sysconfig.get_config_var('Py_GIL_DISABLED') else 'normal'


class Readers:
    """The GIL protects layouts; each recording thread owns its reused walk stack."""
    def __init__(self,module,initial):
        import threading
        self.module=module
        self.local=threading.local()
        self.local.reader=initial
    def capture(self,value,budget=None,partial_ok=True):
        reader=getattr(self.local,'reader',None)
        if reader is None:
            reader=self.local.reader=Reader(self.module)
        return reader.capture(value,budget,partial_ok)


def select_reader(module=None):
    """A failed import, unsupported build, or failed self-check keeps the oracle."""
    import sys
    if sys.implementation.name!='cpython' or sys.version_info[:2] not in ((3,12),(3,13)):
        return None,'native requires CPython 3.12 or 3.13'
    if python_build()!='normal':
        return None,'native does not support a free-threaded Python build'
    try:
        if module is None:
            import importlib
            module=importlib.import_module('ldr._native')
        if not hasattr(module,'ReaderCore'):
            return None,'native module not built'
        if module.watcher_count()!=1:
            return None,'native type watcher unavailable'
        engine=Reader(module)
        _self_check(engine)
        return Readers(module,engine),None
    except Exception as error:
        return None,f'native load/self-check failed: {type(error).__name__}: {error}'


def _self_check(engine):
    import collections
    import hashlib
    class DictValue: pass
    class SlotValue: __slots__=('present','absent')
    class Both: __slots__=('slot','__dict__')
    class Kind(enum.Enum): ITEM=1
    managed=DictValue();managed.field='value'
    slotted=SlotValue();slotted.present=17
    both=Both();both.slot=8;both.extra=9
    cycle=[];cycle.append(cycle)
    values=[managed,slotted,both,collections.namedtuple('Point','x y')(1,2),
            ValueError('bad',17),Kind.ITEM,'é日本','\ud800',cycle]
    stats={name:getattr(fp.STATS,name) for name in fp.STATS.__slots__}
    try:
        for value in values:
            out=hashlib.blake2b(digest_size=fp.DIGEST_BYTES)
            walk=fp._Walk(fp.DEFAULT_BUDGET,True)
            normalized=fp.STATS.tmp_normalized
            status='complete'
            try: walk.encode(value,out)
            except fp._Stop as error: status=error.status
            expected={'digest':out.hexdigest(),'visited':walk.visited,'status':status,
                      'partial':walk.partial,'immutable':walk.immutable,
                      'normalized':fp.STATS.tmp_normalized-normalized,'over_ceiling':False}
            actual=engine.capture(value)
            for field,wanted in expected.items():
                if actual[field]!=wanted:
                    raise ValueError(f'self-check {type(value).__name__}: {field} differs')
    finally:
        for name,value in stats.items(): setattr(fp.STATS,name,value)


def reader_metadata():
    from . import __version__, fingerprint as fp
    reader=fp._native_reader
    name='native' if reader is not None else 'python'
    return {'signature_reader':{'name':name,**({'reason':fp._native_reason} if reader is None else {})},
            'reader_versions':{'ldr':__version__,'native':getattr(reader.module,'__version__',None) if reader is not None else None},
            'python_build':python_build()}
