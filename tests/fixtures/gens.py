def gen(n):
    for i in range(n):
        yield i * 2


def after(total):
    return total + 1


def consume():
    total = 0
    for v in gen(3):
        total += v
    return after(total)
