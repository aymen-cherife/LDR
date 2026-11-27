import pytest


def test_coverage_detects_an_omitted_position():
    from signature_coverage import check
    from ldr import fingerprint as fp
    original = fp._Walk.encode
    def broken(walk, value, out):
        return original(walk, value[:-1], out)
    with pytest.raises(AssertionError):
        check([1, 2, 3], encode=broken)


def test_coverage_includes_every_large_list_position():
    from signature_coverage import check
    result = check(list(range(2_000_000)))
    assert result['positions'] == 2_000_001
    assert result['missing'] == 0


def test_rich_positions():
    from signature_coverage import check
    from rich.console import Console
    from rich.segment import Segment
    from rich.style import Style
    from rich.text import Text
    from rich.theme import Theme, ThemeStack
    for value in [ThemeStack(Theme({'x': Style(color='red')})), Segment('text', Style(bold=True)),
                  Style(color='red'), Text('text'), Console(width=40)]:
        assert check(value)['missing'] == 0


def test_nested_cycle_and_redacted_positions():
    from signature_coverage import check
    cycle = [1]
    cycle.append(cycle)
    for value in [cycle, {'a': [1, (2,3)], 'b': {4,5}}, {'api_key':'secret'}, [[1],[2]]]:
        assert check(value)['missing'] == 0


def test_one_mutation_per_wire_type():
    import datetime as dt
    from decimal import Decimal
    from functools import partial
    from signature_coverage import check
    class Slots:
        __slots__ = ('value',)
    empty, present = Slots(), Slots()
    present.value = None
    class Other: pass
    pairs = [(None,1),(False,True),(1,2),(2**80,2**81),(0.0,-0.0),(1+2j,1+3j),
             ('a','b'),(b'a',b'b'),(bytearray(b'a'),bytearray(b'b')),
             (Decimal('1.1'),Decimal('1.2')),([1],[2]),((1,),(2,)),({'a':1},{'b':1}),
             ({1},{2}),(frozenset({1}),frozenset({2})),(Slots,Other),
             (len,abs),(dt.date(2026,1,1),dt.date(2026,1,2)),
             (dt.time(1),dt.time(2)),(dt.datetime(2026,1,1),dt.datetime(2026,1,2)),
             (dt.timedelta(seconds=1),dt.timedelta(seconds=2)),(range(3),range(4)),
             (Ellipsis,NotImplemented),(NotImplemented,None),(empty,present),
             (partial(pow,2,exp=3),partial(pow,2,exp=4))]
    class Receiver:
        def first(self): pass
        def second(self): pass
    obj=Receiver()
    pairs.extend([(obj.first,obj.second),(Receiver.first,Receiver.second),([1]*40,[2]*40),([1,2.0],[1,3.0])])
    for before,after in pairs:
        assert check(before)['bytes'] != check(after)['bytes'], type(before)


def test_excluded_slots_and_normalized_paths_are_marked():
    import tempfile
    import os
    from signature_coverage import check
    class Slots:
        __slots__=('value','__weakref__')
    Slots.value=property(lambda self: (_ for _ in ()).throw(AssertionError('descriptor ran')))
    result=check(Slots())
    assert any(row[3]=='left-out:non-member-descriptor' for row in result['log'])
    assert any(row[3]=='left-out:layout-declaration' for row in result['log'])
    result=check(os.path.join(tempfile.gettempdir(),'tmpcoverage','out.csv'))
    assert result['log'][0][3]=='normalized-temporary-path'
