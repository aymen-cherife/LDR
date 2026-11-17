def consume(items):
    return len(items)


def accept(value):
    return None


class Account:
    def __init__(self, balance, guard=None):
        self.balance = balance
        self.guard = guard

    def label(self, prefix):
        return prefix
