"""Speed step 4: functions, functools.partial and bound methods are read, not 'partially read'."""

import functools

from ldr.fingerprint import PARTIAL_MARK, fingerprint, is_digest

SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def add(a, b=0, *, scale=1):
    return (a + b) * scale


def other(a, b=0, *, scale=1):
    return a - b


class Counter:
    def __init__(self, start):
        self.value = start

    def bump(self, by):
        self.value += by


def read(value, **kwargs):
    token = fingerprint(value, **kwargs)
    assert is_digest(token) and not token.endswith(PARTIAL_MARK), token
    return token


def test_a_partial_is_read_as_a_whole_without_partial_ok():
    assert read(functools.partial(add, 1)) == read(functools.partial(add, 1))
    assert read(functools.partial(add, 1)) != read(functools.partial(add, 2))
    assert read(functools.partial(add, 1)) != read(functools.partial(other, 1))
    assert read(functools.partial(add, 1, scale=2)) != read(functools.partial(add, 1, scale=3))
    assert read(functools.partial(add, 1)) != read(add)
    assert read(functools.partial(add, [1, 2])) != read(functools.partial(add, [1, 3]))


def test_partial_keyword_order_does_not_matter():
    first = functools.partial(add, 1, scale=2, b=3)
    second = functools.partial(add, 1, b=3, scale=2)
    assert read(first) == read(second)


def test_a_partial_of_a_partial_and_a_partial_inside_a_container():
    inner = functools.partial(add, 1)
    assert read(functools.partial(inner, 2)) == read(functools.partial(add, 1, 2))
    assert read({"callback": functools.partial(add, 1)}) == read({"callback": functools.partial(add, 1)})
    assert read({"callback": functools.partial(add, 1)}) != read({"callback": functools.partial(add, 2)})


def test_arguments_inside_a_partial_are_redacted_like_any_other_value():
    assert fingerprint(functools.partial(add, SECRET)) == "redacted"
    assert fingerprint(functools.partial(add, 1, b=SECRET)) == "redacted"
    assert fingerprint(functools.partial(add, 1, api_key="not-a-pattern")) == "redacted"  # secret name
    assert fingerprint(functools.partial(functools.partial(add, SECRET), 2)) == "redacted"
    assert fingerprint([functools.partial(add, SECRET)]) == "redacted"


def test_functions_and_bound_methods_are_named():
    assert read(add) == read(add) != read(other)
    counter = Counter(1)
    assert read(counter.bump) == read(counter.bump)
    assert read(counter.bump) != read(Counter.bump)  # a bound method is not the plain function
    assert read(len) == read(len)


def test_a_bound_method_is_its_name_and_its_receivers_type_never_the_receiver():
    first, second = Counter(1), Counter(2)
    assert read(first.bump) == read(second.bump)  # same method of the same type: the object is not read
    first.value = [object() for _ in range(10_000)]  # would be walked if the receiver were read
    assert read(first.bump) == read(second.bump)

    class Other(Counter):
        pass

    assert read(Other(1).bump) != read(first.bump)  # another receiver type


def test_a_method_of_a_builtin_object_is_its_name_only():
    assert read([1].append) == read([2].append) == read([].append)
    assert read([1].append) != read({}.get)
    assert read("abc".upper) == read("abd".upper)
    assert fingerprint([SECRET].append) != "redacted"  # nothing inside the receiver is read


def test_secrets_in_a_partial_inside_a_bound_container_are_still_redacted():
    assert fingerprint({"callback": functools.partial(add, SECRET)}) == "redacted"
