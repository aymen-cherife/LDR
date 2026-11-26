"""Read an object's state without running any of its code.

Everything here goes through C-level descriptors (``type.__dict__[...]``,
slot ``member_descriptor`` objects, the built-in ``__dict__`` getter) rather
than ``getattr``/``vars``/``repr``/``str``. A user class that defines
``__repr__``, ``__getattr__``, ``__getstate__``, properties, ``__iter__``,
``__len__``, ``__eq__`` or ``__hash__`` therefore cannot run, block or mutate
anything while LDR records it.
"""

from __future__ import annotations

import types
from typing import Any, NamedTuple
from weakref import ref

_TYPE_DICT = type.__dict__["__dict__"].__get__
_TYPE_MRO = type.__dict__["__mro__"].__get__
_TYPE_NAME = type.__dict__["__name__"].__get__
_TYPE_QUALNAME = type.__dict__["__qualname__"].__get__
_TYPE_MODULE = type.__dict__["__module__"].__get__
_BASE_EXC_ARGS = BaseException.__dict__["args"].__get__

_BUILTIN_BASES = (dict, list, tuple, set, frozenset)
_ABSENT = object()
UNSET = object()


class Reading(NamedTuple):
    label: str  # class name, for previews
    identity: str  # module + qualified name, for fingerprints
    # (name, value) pairs. ``name`` is None for positional content, such as the
    # items of a list subclass.
    fields: list[tuple[str | None, Any]]


def _safe(call, *args, default=""):
    try:
        result = call(*args)
    except Exception:
        return default
    return result if type(result) is str else default


def type_label(cls: type) -> str:
    return _safe(_TYPE_NAME, cls, default="object")


def type_identity(cls: type) -> str:
    return f"{_safe(_TYPE_MODULE, cls)}.{_safe(_TYPE_QUALNAME, cls, default=type_label(cls))}"


class _Owner(NamedTuple):
    owner: ref
    slots: object
    private_name: str | None
    members: tuple
    dictionary: int
    named_fields: tuple | None


class _Reader(NamedTuple):
    owners: tuple[_Owner, ...]
    names: tuple[str, str, str]
    identity: str
    base: type | None
    named_fields: tuple | None
    exception: bool
    dictionary: int | None
    has_state: bool


# Neither the keys nor the stored descriptors hold classes alive. Use identity
# rather than hashing the class, which could invoke its metaclass.
_readers: dict[int, _Reader] = {}


def _slots(value):
    if value is _ABSENT or type(value) is str:
        return value
    if type(value) is tuple and all(type(name) is str for name in value):
        return value
    if type(value) in (tuple, list):
        return tuple(name for name in value if type(name) is str)
    return None


def _slot_names(slots):
    if type(slots) is str:
        names = [slots]
    elif type(slots) is tuple:
        names = slots
    else:
        return ()
    return tuple(name for name in names if name not in ("__dict__", "__weakref__"))


def _names(cls):
    label = type_label(cls)
    return label, _safe(_TYPE_MODULE, cls), _safe(_TYPE_QUALNAME, cls, default=label)


def _named_fields(value):
    return value if type(value) is tuple and all(type(name) is str for name in value) else None


def _discover(cls, mro, names):
    key = id(cls)

    def forget(owner):
        current = _readers.get(key)
        if current is not None and current.owners[0].owner is owner:
            _readers.pop(key, None)

    base = next((base for base in _BUILTIN_BASES if any(base is owner for owner in mro)), None)
    owners = []
    dictionary = None
    named_fields = None
    for index, klass in enumerate(mro):
        namespace = _TYPE_DICT(klass)
        slots = _slots(namespace.get("__slots__", _ABSENT))
        private_name = None
        members = []
        for name in _slot_names(slots):
            stored = name
            if name.startswith("__") and not name.endswith("__"):
                if private_name is None:
                    private_name = type_label(klass)
                stored = "_" + private_name.lstrip("_") + name
            descriptor = namespace.get(stored)
            members.append((name, stored, id(descriptor), type(descriptor) is types.MemberDescriptorType))
        descriptor = namespace.get("__dict__")
        if dictionary is None and type(descriptor) is types.GetSetDescriptorType:
            dictionary = index
        fields = _named_fields(namespace.get("_fields")) if base is tuple else None
        if named_fields is None:
            named_fields = fields
        owners.append(_Owner(ref(klass, forget) if index == 0 else ref(klass), slots, private_name,
                             tuple(members), id(descriptor), fields))

    _, module, qualified = names
    reader = _Reader(tuple(owners), names, f"{module}.{qualified}", base, named_fields,
                     any(klass is BaseException for klass in mro), dictionary,
                     dictionary is not None or any(owner.slots is not _ABSENT for owner in owners)
                     or len(mro) == 1)
    _readers[key] = reader
    return reader


def _layout(cls):
    mro = _TYPE_MRO(cls)
    reader = _readers.get(id(cls))
    names = _names(cls)
    if reader is not None and reader.names == names and len(reader.owners) == len(mro):
        namespaces = []
        for klass, owner in zip(mro, reader.owners):
            namespace = _TYPE_DICT(klass)
            slots = namespace.get("__slots__", _ABSENT)
            if (owner.owner() is not klass or
                    (slots is not owner.slots and _slots(slots) != owner.slots) or
                    id(namespace.get("__dict__")) != owner.dictionary or
                    (owner.private_name is not None and type_label(klass) != owner.private_name) or
                    (reader.base is tuple and namespace.get("_fields") is not owner.named_fields
                     and _named_fields(namespace.get("_fields")) != owner.named_fields)):
                break
            namespaces.append(namespace)
        else:
            return reader, namespaces
    return _discover(cls, mro, names), tuple(map(_TYPE_DICT, mro))


def _read_dict(value, reader, namespaces):
    if reader.dictionary is None:
        return None
    try:
        descriptor = namespaces[reader.dictionary]["__dict__"]
        if type(descriptor) is types.GetSetDescriptorType:
            state = descriptor.__get__(value)
            return state if type(state) is dict else None
    except Exception:
        return None
    return None


def instance_dict(value: Any) -> dict | None:
    """The instance ``__dict__`` through the built-in getter only, else None."""
    try:
        reader, namespaces = _layout(type(value))
        return _read_dict(value, reader, namespaces)
    except Exception:
        return None


def _builtin_content(value: Any, base: type) -> list[tuple[str | None, Any]]:
    """Items of a builtin-container subclass, read with the base type's methods."""
    if base is dict:
        return [(None, pair) for pair in tuple(dict.items(value))]
    if base is list:
        return [(None, item) for item in tuple(list.__iter__(value))]
    if base is tuple:
        return [(None, item) for item in tuple.__iter__(value)]
    iterator = set.__iter__ if base is set else frozenset.__iter__
    return [(None, item) for item in tuple(iterator(value))]


def read(value: Any, *, include_unset: bool = False, unset_marker: Any = UNSET, extra_slots: set[int] | None = None) -> Reading | None:
    """Structural reading of a non-builtin instance, or None if unreadable."""
    cls = type(value)
    try:
        reader, namespaces = _layout(cls)
        fields: list[tuple[str | None, Any]] = []
        added_slots: set[int] = set() if extra_slots is None else extra_slots

        if reader.base is not None:
            content = _builtin_content(value, reader.base)
            names = reader.named_fields
            if names is not None and len(names) == len(content):
                fields = [(name, item) for name, (_, item) in zip(names, content, strict=True)]
            else:
                fields = content

        if reader.exception:
            fields.append(("args", _BASE_EXC_ARGS(value)))

        if include_unset:
            # Member descriptors describe physical slots even when __slots__ is edited.
            for owner, namespace in zip(reversed(reader.owners), reversed(namespaces)):
                klass = owner.owner()
                if klass is None:
                    return None
                declared = {stored: name for name, stored, _, _ in owner.members}
                prefix = "_" + type_label(klass).lstrip("_") + "__"
                for stored, descriptor in namespace.items():
                    if stored in ("__dict__", "__weakref__") or type(descriptor) is not types.MemberDescriptorType:
                        continue
                    name = declared.get(stored)
                    if name is None:
                        name = "__" + stored[len(prefix):] if stored.startswith(prefix) and not stored.endswith("__") else stored
                        added_slots.add(len(fields))
                    try:
                        fields.append((name, descriptor.__get__(value)))
                    except AttributeError:
                        fields.append((name, unset_marker))
        else:
            replaced = False
            for owner, namespace in zip(reversed(reader.owners), reversed(namespaces)):
                for name, stored, original, member in owner.members:
                    descriptor = namespace.get(stored)
                    if id(descriptor) != original:
                        replaced = True
                        member = type(descriptor) is types.MemberDescriptorType
                    if member:
                        try:
                            fields.append((name, descriptor.__get__(value)))
                        except AttributeError:
                            if include_unset:
                                fields.append((name, unset_marker))
                            continue  # slot declared but never assigned
            if replaced:
                _discover(cls, _TYPE_MRO(cls), _names(cls))
        state = _read_dict(value, reader, namespaces)
        if state is not None:
            fields.extend((key, item) for key, item in tuple(state.items()) if type(key) is str)

        if state is None and not reader.has_state and all(index in added_slots for index in range(len(fields))):
            return None
        return Reading(reader.names[0], reader.identity, fields)
    except Exception:
        return None
