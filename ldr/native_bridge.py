"""Prototype boundary for nodes the native reader cannot yet encode exactly."""
import re
import functools,decimal,datetime,types,abc
SPECIAL_TYPES=(functools.partial,types.MethodType,types.FunctionType,types.BuiltinFunctionType,decimal.Decimal,datetime.date,datetime.time,datetime.datetime,datetime.timedelta,type,abc.ABCMeta,range,type(Ellipsis),type(NotImplemented))
from . import fingerprint as fp, redact


class Sink:
    def __init__(self): self.data=bytearray()
    def update(self,data): self.data.extend(data)
    def take(self):
        result=bytes(self.data);self.data.clear();return result


class Fallback:
    def __init__(self):
        self.walk=fp._Walk(fp.DEFAULT_BUDGET,True)
        self.sink=Sink()
    def secret_key(self,value):
        return fp.is_secret_name(value)
    def secret_text(self,value):
        return fp._looks_secret(value)
    def start(self,value,active,visited,partial_ok):
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


class Reader:
    def __init__(self,module):
        self.module=module;self.token=None;self.core=None
    def refresh(self):
        token=redact.text_policy()
        if self.core is None or not redact.same_text_policy(self.token):
            patterns=[('(?i)' if pattern.flags & re.IGNORECASE and not pattern.pattern.startswith('(?i)') else '')+pattern.pattern for _,pattern,_ in redact._SHAPES]
            self.core=self.module.ReaderCore(fp._TMP_PREFIXES,
                [s.encode('utf8','surrogatepass') for s,_ in redact._environment[0]],
                [(s.encode('utf8','surrogatepass'),len(s)>=8) for s in redact._argv_secrets[1]],patterns,Fallback(),SPECIAL_TYPES)
            self.token=token
    def walk(self,value,budget=None,partial_ok=True):
        self.refresh()
        normalized=fp.STATS.tmp_normalized
        try:
            return self.core.walk(value,partial_ok)
        finally:
            fp.STATS.tmp_normalized=normalized
    def stats(self):
        return self.core.stats()
