"""Independent v2 wire oracle and path log. No production traversal helpers."""
import abc
import datetime as dt
import decimal
import functools
import hashlib
import math
import struct
import types
import sys
from collections import Counter, deque
from types import SimpleNamespace

from ldr import fingerprint as fp, redact

REAL_HASH = hashlib.blake2b
TOTAL = Counter()
UNSET = object()


def integer(n):
    if -(1 << 63) <= n < 1 << 63:
        return b'i' + n.to_bytes(8, 'little', signed=True)
    size = ((n if n >= 0 else ~n).bit_length() + 8) // 8
    return b'I' + size.to_bytes(8, 'little') + n.to_bytes(size, 'little', signed=True)


def text(tag, value):
    return tag + len(value).to_bytes(8, 'little') + value


def identity(cls):
    return type.__getattribute__(cls, '__module__') + '.' + type.__getattribute__(cls, '__qualname__')


def fields(value):
    """Enumerate instance content from CPython descriptors, independently of structure.read."""
    cls = type(value)
    owners = type.__getattribute__(cls, '__mro__')
    namespaces = [type.__getattribute__(owner, '__dict__') for owner in owners]
    result = []
    base = next((b for b in (dict, list, tuple, set, frozenset) if b in owners), None)
    if base:
        if base is dict:
            for key, item in dict.items(value):
                result.append((None, (key, item), 'item'))
        else:
            names = next((ns['_fields'] for ns in namespaces if type(ns.get('_fields')) is tuple), None)
            iterator = {list:list.__iter__, tuple:tuple.__iter__, set:set.__iter__, frozenset:frozenset.__iter__}[base]
            for index, item in enumerate(iterator(value)):
                result.append((names[index] if names else None, item, 'item'))
    if BaseException in owners:
        result.append(('args', BaseException.__dict__['args'].__get__(value), 'field'))
    has_state = len(owners) == 1
    for owner, ns in reversed(list(zip(owners, namespaces))):
        slots = ns.get('__slots__', ())
        has_state |= '__slots__' in ns
        if type(slots) is str:
            slots = (slots,)
        if not isinstance(slots, (tuple, list)):
            slots = ()
        declared = {}
        for name in slots:
            if type(name) is not str or name in ('__weakref__', '__dict__'):
                continue
            stored = '_' + owner.__name__.lstrip('_') + name if name.startswith('__') and not name.endswith('__') else name
            declared[stored] = name
        python_slots = type not in owners and bool(type.__getattribute__(owner, '__flags__') & 512) and '__slots__' in ns
        candidates = ns.items() if python_slots else ((stored, ns.get(stored)) for stored in declared)
        for stored, descriptor in candidates:
            if stored in ('__dict__', '__weakref__') or type(descriptor) is not types.MemberDescriptorType:
                continue
            if python_slots and descriptor.__objclass__ is not owner:
                continue
            prefix = '_' + owner.__name__.lstrip('_') + '__'
            name = declared.get(stored, '__' + stored[len(prefix):] if stored.startswith(prefix) and not stored.endswith('__') else stored)
            try:
                item = descriptor.__get__(value)
            except AttributeError:
                if not python_slots:
                    continue
                item = UNSET
            result.append((name, item, 'slot'))
    for ns in namespaces:
        descriptor = ns.get('__dict__')
        if type(descriptor) is types.GetSetDescriptorType:
            has_state = True
            try:
                state = descriptor.__get__(value)
            except (AttributeError, TypeError):
                continue
            if type(state) is dict:
                has_state = True
                result.extend((name, item, 'field') for name, item in state.items() if type(name) is str)
                break
    return result if result or has_state else None


def omitted_positions(value):
    """Name excluded declared slots without invoking their descriptors."""
    for owner in type.__getattribute__(type(value), '__mro__'):
        ns = type.__getattribute__(owner, '__dict__')
        slots = ns.get('__slots__', ())
        if type(slots) is str:
            slots = (slots,)
        if type(slots) not in (tuple, list):
            continue
        for name in slots:
            if type(name) is not str:
                continue
            stored = '_' + owner.__name__.lstrip('_') + name if name.startswith('__') and not name.endswith('__') else name
            if name in ('__dict__', '__weakref__'):
                yield name, 'left-out:layout-declaration'
            elif type(ns.get(stored)) is not types.MemberDescriptorType:
                yield name, 'left-out:non-member-descriptor'


class Recorder:
    def __init__(self, *args, children=None, **kwargs):
        self.data = bytearray(args[0] if args else b'')
        self.children = deque()
        self.log = []
        self.offset = 0
        self.error = None
    def update(self, data):
        self.data.extend(data)
    def digest(self):
        return REAL_HASH(self.data, digest_size=16).digest()
    def hexdigest(self):
        return self.digest().hex()
    def position(self, path, expected, status='encoded', partial=False):
        end = self.offset + len(expected)
        available = bytes(self.data[self.offset:end])
        if self.error and end > len(self.data):
            assert expected.startswith(available), path
            end = len(self.data)
            status = 'left-out:' + self.error
        else:
            assert available == expected, (path, 'position bytes differ or missing')
        self.log.append((path, self.offset, end-self.offset, status))
        self.offset = end


def oracle(value, root, children, partial=False):
    """Independent DFS, yielding one position per node plus named field keys."""
    active = {}
    tasks = [('node', value, None, root)]
    count = 0
    while tasks:
        operation, value, path, target = tasks.pop()
        if operation == 'items':
            try: index,item=next(value)
            except StopIteration: continue
            tasks.append(('items',value,path,target))
            tasks.append(('node',item,(path,'item',index),target))
            continue
        if operation == 'leave':
            del active[value]
            continue
        if operation == 'mark':
            target.position(path,b'',value)
            continue
        if operation == 'bytes':
            target.position(path, value)
            continue
        if operation == 'setend':
            tag, parts = value
            target.position(path, tag + len(parts).to_bytes(8,'little') + b''.join(sorted(part.digest() for part in parts)))
            continue
        count += 1
        kind = type(value)
        data = None
        status = 'encoded'
        nested = []
        if value is UNSET: data = b'u'; status = 'unset'
        elif value is None: data = b'N'
        elif kind is bool: data = b'T' if value else b'F'
        elif kind is int: data = integer(value)
        elif kind in (float, complex):
            def number(x):
                return bytes.fromhex('000000000000f87f') if math.isnan(x) else struct.pack('<d', x)
            data = b'f'+number(value) if kind is float else b'c'+number(value.real)+number(value.imag)
        elif kind is str:
            normalized = fp.normalize_temp_paths(value)
            status = 'normalized-temporary-path' if normalized != value else 'encoded'
            if fp._looks_secret(value): status = 'redacted'; data = b''
            else: data = text(b's', normalized.encode('utf8','surrogatepass'))
        elif kind in (bytes,bytearray):
            if fp._looks_secret(bytes(value).decode('latin1')): status='redacted'; data=b''
            else: data = text(b'b' if kind is bytes else b'B',bytes(value))
        elif kind is decimal.Decimal: data=text(b'D',str(value).encode())
        elif kind in (type,abc.ABCMeta): data=text(b'C',identity(value).encode())
        elif kind in (types.FunctionType,types.BuiltinFunctionType): data=text(b'Y',(str(value.__module__)+'.'+value.__qualname__).encode('utf8','surrogatepass'))
        elif kind in (dt.datetime,dt.date,dt.time,dt.timedelta):
            if kind is dt.timedelta: data=b'Mt'+struct.pack('<qII',value.days,value.seconds,value.microseconds)
            else:
                data=b'M'+{dt.datetime:b'D',dt.date:b'd',dt.time:b'T'}[kind]
                if kind is not dt.time: data+=struct.pack('<HBB',value.year,value.month,value.day)
                if kind is not dt.date:
                    data+=struct.pack('<BBBI',value.hour,value.minute,value.second,value.microsecond)
                    offset=value.utcoffset()
                    data+=b'n' if offset is None else b'z'+int(offset.total_seconds()*1_000_000).to_bytes(8,'little',signed=True)
        elif kind is range: data=b'R'+integer(value.start)+integer(value.stop)+integer(value.step)
        elif value is Ellipsis: data=b'E'
        elif value is NotImplemented: data=b'X'
        elif kind is types.MethodType:
            data=b'm'
            nested=[('node',value.__func__,(path,'field','function'),target),('bytes',text(b'T',identity(type(value.__self__)).encode()),(path,'left-out','receiver-value'),target)]
        elif kind is functools.partial:
            data=b'P'
            nested=[('node',value.func,(path,'field','func'),target),('node',value.args,(path,'field','args'),target),('bytes',len(value.keywords).to_bytes(8,'little'),(path,'field','keywords'),target)]
            for name in sorted(value.keywords):
                nested.extend([('bytes',text(b'n',name.encode()),(path,'key',name),target),('node',value.keywords[name],(path,'field',name),target)])
        else:
            marker=id(value)
            if marker in active:
                data=b'@'+(len(active)-active[marker]).to_bytes(8,'little');status='cycle'
            else:
                active[marker]=len(active)
                tasks.append(('leave',marker,None,target))
                if kind in (list,tuple):
                    data=(b'l' if kind is list else b't')+len(value).to_bytes(8,'little')
                    # Iterator tasks keep the 2M-item corpus linear in memory.
                    nested=[('items',iter(enumerate(value)),path,target)]
                elif kind is dict:
                    data=b'd'+len(value).to_bytes(8,'little')
                    for index,(key,item) in enumerate(value.items()):
                        nested.extend([('node',key,(path,'key',index),target),('node',item,(path,'item',index),target)])
                elif kind in (set,frozenset):
                    parts=[]
                    for index,item in enumerate(value):
                        assert target.children, 'missing element hash stream'
                        part=target.children.popleft();parts.append(part)
                        nested.append(('node',item,(path,'element',index),part))
                    nested.append(('setend',(b'e' if kind is set else b'z',parts),path,target))
                    data=b''
                else:
                    state=fields(value)
                    for name,reason in omitted_positions(value):
                        nested.append(('mark',reason,(path,'slot',name),target))
                    if state is None:
                        data=text(b'?',identity(kind).encode('utf8','surrogatepass')) if partial else b''
                        status='left-out:unsupported'
                    else:
                        data=text(b'O',identity(kind).encode('utf8','surrogatepass'))+len(state).to_bytes(8,'little')
                        for index,(name,item,position_kind) in enumerate(state):
                            nested.append(('bytes',b'p' if name is None else text(b'n',name.encode('utf8','surrogatepass')),(path,'key',name if name is not None else index),target))
                            if item is not UNSET: nested.append(('bytes',b'v',(path,'presence',index),target))
                            nested.append(('node',item,(path,position_kind,name if name is not None else index),target))
        target.position(path,data,status)
        tasks.extend(reversed(nested))
    return count


def check(value, *, encode=None, budget=None, partial_ok=True, walk=None):
    children=deque()
    def factory(*args,**kwargs):
        recorder=Recorder(*args,**kwargs)
        parent=sys._getframe(1).f_locals['out']
        parent.children.append(recorder)
        children.append(recorder)
        return recorder
    output=Recorder()
    walk=walk or fp._Walk(budget or fp.DEFAULT_BUDGET,partial_ok)
    saved=fp.hashlib
    error=None
    try:
        fp.hashlib=SimpleNamespace(blake2b=factory)
        try: (encode or fp._Walk.encode)(walk,value,output)
        except fp._Stop as exc: error=exc
    finally: fp.hashlib=saved
    normalized=fp.STATS.tmp_normalized
    try:
        for recorder in [output,*children]:
            recorder.error=error.status if error else None
        count=oracle(value,output,children,partial_ok)
        assert output.offset==len(output.data), 'unattributed bytes'
        assert all(not recorder.children for recorder in [output,*children]), 'unattributed element streams'
        assert all(recorder.offset == len(recorder.data) for recorder in children), 'unattributed element bytes'
    finally: fp.STATS.tmp_normalized=normalized
    count=sum(len(recorder.log) for recorder in [output,*children])
    TOTAL['values']+=1;TOTAL['positions']+=count
    return {'positions':count,'missing':0,'log':output.log,'bytes':output.data,'error':error}
