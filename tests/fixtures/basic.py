import json


def double(x):
    return x * 2


def add_six(x):
    return x + 6


def outer(x):
    return add_six(double(x))


class Calc:
    def __init__(self, base):
        self.base = base

    def scale(self, k):
        return self.base * k


def uses_stdlib():
    return json.loads('{"a": 1}')


def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def deep(n):
    if n == 0:
        return 0
    return deep(n - 1) + 1


def takes_big(data):
    return 1


class Explosive:
    def __repr__(self):
        raise RuntimeError("boom")


def mutates(xs):
    xs.append(99)
    return len(xs)


def rebinds(xs):
    xs = [99]
    return len(xs)
