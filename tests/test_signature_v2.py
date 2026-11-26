import hashlib
import math
import struct

from ldr import fingerprint as fp


class Capture:
    def __init__(self): self.data = bytearray()
    def update(self, value): self.data.extend(value)


def encoded(value):
    output = Capture()
    walk = fp._Walk(fp.DEFAULT_BUDGET)
    walk.encode(value, output)
    return bytes(output.data), walk.visited


def test_v2_scalar_wire_format():
    assert encoded(1)[0] == b'i' + struct.pack('<q', 1)
    assert encoded(-(2**63))[0] == b'i' + struct.pack('<q', -(2**63))
    assert encoded(2**63)[0] == b'I' + struct.pack('<Q', 9) + (2**63).to_bytes(9, 'little', signed=True)
    assert encoded(-0.0)[0] == b'f' + struct.pack('<d', -0.0)
    assert encoded(float('nan'))[0] == b'f' + struct.pack('<Q', 0x7ff8000000000000)
    assert encoded('é\ud800')[0] == b's' + struct.pack('<Q', 5) + 'é\ud800'.encode('utf8', 'surrogatepass')


def test_unset_slots_are_tagged_without_counting_a_node():
    class Slots:
        __slots__ = ('value',)
    obj = Slots()
    before, count = encoded(obj)
    obj.value = None
    after, count_after = encoded(obj)
    assert before != after
    assert count == 1 and count_after == 2


def test_versioned_invocation():
    from ldr.fingerprint import SIGNATURE_VERSION
    assert SIGNATURE_VERSION == 2


def test_previous_version_is_not_compared(tmp_path):
    import json
    import os
    import sqlite3
    import sys
    from ldr import processes
    script = tmp_path / 'example.py'
    script.write_text('def action(x): return x + 1\naction(2)\n')
    env = dict(os.environ, PYTHONPATH=str(__import__('pathlib').Path(__file__).resolve().parents[1]))
    def run():
        return processes.run([sys.executable, '-m', 'ldr', 'run', 'example.py'], cwd=tmp_path,
                             env=env, capture_output=True, text=True, check=True)
    run()
    db = sqlite3.connect(tmp_path / '.ldr' / 'traces.sqlite3')
    row = db.execute('SELECT id,invocation FROM runs').fetchone()
    invocation = json.loads(row[1])
    assert invocation.pop('signature_version') == 2
    db.execute('UPDATE runs SET invocation=? WHERE id=?', (json.dumps(invocation), row[0]))
    db.commit()
    result = run()
    assert 'signature format' in result.stdout.lower()
    payload = json.loads((tmp_path / '.ldr' / 'last.json').read_text())
    assert not payload['status'].get('reference')
    assert 'signature' in payload['status']['previous_unavailable'].lower()


def test_v2_large_negative_integer_minimal():
    value = -(2**71)
    assert encoded(value)[0] == b'I' + struct.pack('<Q', 9) + value.to_bytes(9, 'little', signed=True)


def test_v2_dict_order_and_mutations():
    assert fp.fingerprint({'a': 1, 'b': 2}) != fp.fingerprint({'b': 2, 'a': 1})
    before = fp.fingerprint({'a': [1, 2], 'b': {3, 4}})
    for changed in ({'a': [1, 3], 'b': {3, 4}}, {'a': [1, 2], 'b': {3, 5}},
                    {'c': [1, 2], 'b': {3, 4}}):
        assert before != fp.fingerprint(changed)
    assert fp.fingerprint({'api_key': 'a'}) == fp.fingerprint({'api_key': 'b'}) == fp.REDACTED


def test_signature_is_stable_across_hash_seeds():
    import os, sys
    from ldr import processes
    script = '''from ldr import fingerprint as f
v = {'ordered': ['a', 'b'], 'set': {f'word{i}' for i in range(200)}, 'frozen': frozenset({'x','y','z'})}
print(f.fingerprint(v))
f.configure(20)
print(f.fingerprint({f'word{i}' for i in range(200)}))
'''
    outputs = []
    for seed in ('1', '2'):
        result = processes.run([sys.executable, '-c', script], env=dict(os.environ, PYTHONHASHSEED=seed),
                               capture_output=True, text=True, check=True)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_rich_objects_readable_state_changes():
    from rich.color import Color
    from rich.console import Console
    from rich.segment import Segment
    from rich.style import Style
    from rich.text import Text
    from rich.theme import Theme, ThemeStack
    pairs = [(Style(color='red'), Style(color='blue')),
             (Segment('one', Style(bold=True)), Segment('two', Style(bold=True))),
             (Text('one'), Text('two')),
             (ThemeStack(Theme({'x': Style(color='red')})), ThemeStack(Theme({'x': Style(color='blue')}))),
             (Console(width=40), Console(width=80)),
             (Color.parse('red'), Color.parse('blue'))]
    for left, right in pairs:
        assert fp.fingerprint(left, partial_ok=True) != fp.fingerprint(right, partial_ok=True), type(left)


def test_v1_and_v2_visit_same_nodes():
    # Node counts recorded from 3976cd4 for these values.
    cycle = []
    cycle.append(cycle)
    class Slots:
        __slots__ = ('present', 'unset')
        def __init__(self):
            self.present = [1, 2]
    values = [None, 2**100, [1, 2, 3], {'x': [1, 2]}, {'a', 'b'}, cycle, Slots(), ['x'] * 40]
    for value, expected in zip(values, [1, 1, 4, 5, 3, 2, 4, 41], strict=True):
        walk = fp._Walk(fp.DEFAULT_BUDGET)
        walk.encode(value, hashlib.blake2b(digest_size=16))
        assert walk.visited == expected


def test_value_redaction_decision_is_not_cached():
    from ldr import redact
    secret = 'private-for-v2-redaction-632941'
    try:
        redact.load_environment({'API_KEY': secret})
        assert fp.fingerprint(secret) == fp.REDACTED
        assert all(value[0] != fp.REDACTED for value in fp._value_cache.values())
    finally:
        redact.load_environment()


def test_store_records_version_for_direct_start(tmp_path):
    import json
    from ldr.sqlite_store import TraceStore
    store = TraceStore.start(tmp_path / '.ldr', {'runner': 'run'}, 100, 1)
    import sqlite3
    with sqlite3.connect(store.path) as connection:
        invocation, = connection.execute('SELECT invocation FROM runs WHERE id=?', (store.run_id,)).fetchone()
    assert json.loads(invocation)['signature_version'] == 2


def test_previous_matching_test_with_other_format_is_not_used(tmp_path):
    import json
    import sqlite3
    from ldr import previous
    from ldr.sqlite_store import TraceStore
    path = tmp_path / '.ldr'
    old = TraceStore.start(path, {'runner': 'pytest'}, 100, 1)
    new = TraceStore.start(path, {'runner': 'pytest'}, 100, 1)
    with sqlite3.connect(old.path) as connection:
        old_invocation, = connection.execute('SELECT invocation FROM runs WHERE id=?', (old.run_id,)).fetchone()
        old_invocation = json.loads(old_invocation)
        old_invocation.pop('signature_version')
        connection.execute('UPDATE runs SET invocation=?, status=? WHERE id=?',
                           (json.dumps(old_invocation), '{}', old.run_id))
        for run in (old.run_id, new.run_id):
            connection.execute('INSERT INTO run_tests (run_id, worker, position, node_id) VALUES (?, ?, ?, ?)',
                               (run, 'main', 0, 'tests/test_sample.py::test_one'))
        assert previous.run_for_tests(connection, new.run_id, ['tests/test_sample.py::test_one']) == (None, None)
        assert previous.incompatible_previous(connection, new.run_id) == 'signature format v1 is incompatible with v2'


def test_stored_object_equal_to_unset_marker_is_still_read():
    from ldr.structure import UNSET
    class Item:
        __slots__ = ('value',)
    item = Item()
    empty, empty_nodes = encoded(item)
    item.value = UNSET
    present, present_nodes = encoded(item)
    assert empty != present
    assert (empty_nodes, present_nodes) == (1, 2)


def test_explicit_reference_version_mismatch_is_explained(tmp_path):
    import json
    import sqlite3
    from ldr import previous
    from ldr.sqlite_store import TraceStore
    old = TraceStore.start(tmp_path / '.ldr', {'runner': 'run'}, 100, 1)
    new = TraceStore.start(tmp_path / '.ldr', {'runner': 'run'}, 100, 1)
    with sqlite3.connect(old.path) as connection:
        row, = connection.execute('SELECT invocation FROM runs WHERE id=?', (old.run_id,)).fetchone()
        invocation = json.loads(row)
        invocation.pop('signature_version')
        connection.execute('UPDATE runs SET invocation=? WHERE id=?', (json.dumps(invocation), old.run_id))
        assert previous.incompatible_reference(connection, new.run_id, old.run_id) == 'signature format v1 is incompatible with v2'


def test_signature_reads_real_member_slots_after_in_place_declaration_edit():
    class Slots:
        __slots__ = ['visible', 'other']
    value = Slots()
    value.visible = 1
    before = fp.fingerprint(value)
    Slots.__slots__.remove('visible')
    assert fp.fingerprint(value) == before
    value.visible = 2
    assert fp.fingerprint(value) != before


def test_datetime_timedelta_and_range_use_binary_components():
    import datetime
    assert encoded(datetime.date(2026, 1, 2))[0] == b'Md' + struct.pack('<HBB', 2026, 1, 2)
    assert encoded(datetime.timedelta(days=-2, seconds=3, microseconds=4))[0] == b'Mt' + struct.pack('<qII', -2, 3, 4)
    assert encoded(range(1, 8, 2))[0] == b'R' + b'i' + struct.pack('<q', 1) + b'i' + struct.pack('<q', 8) + b'i' + struct.pack('<q', 2)


def test_float_nan_canonicalization_and_signed_zero():
    nan1 = struct.unpack('<d', struct.pack('<Q', 0x7ff8000000000001))[0]
    nan2 = struct.unpack('<d', struct.pack('<Q', 0xfff8000000000011))[0]
    assert encoded(nan1)[0] == encoded(nan2)[0]
    assert fp.fingerprint(nan1) == fp.fingerprint(nan2)
    assert fp.fingerprint(0.0) != fp.fingerprint(-0.0)


def test_each_item_key_and_slot_mutation_changes_signature():
    class Slots:
        __slots__ = ('a', 'b', '__dict__')
    value = Slots()
    value.a, value.b, value.extra = 1, 2, 3
    original = fp.fingerprint(value)
    for field in ('a', 'b', 'extra'):
        old = getattr(value, field)
        setattr(value, field, old + 1)
        assert fp.fingerprint(value) != original
        setattr(value, field, old)
    sequence = list(range(12))
    original = fp.fingerprint(sequence)
    for index in range(len(sequence)):
        changed = sequence.copy()
        changed[index] += 100
        assert fp.fingerprint(changed) != original
    dictionary = {'a': 1, 'b': 2, 'c': 3}
    original = fp.fingerprint(dictionary)
    for key in dictionary:
        changed = dictionary.copy()
        changed[key] += 1
        assert fp.fingerprint(changed) != original
        changed = {(name if name != key else name + 'x'): item for name, item in dictionary.items()}
        assert fp.fingerprint(changed) != original
    values = {1, 2, 3}
    original = fp.fingerprint(values)
    for value in values:
        assert fp.fingerprint((values - {value}) | {value + 10}) != original


def test_previous_format_then_new_format_is_compared(tmp_path):
    import json, os, sqlite3, sys
    from pathlib import Path
    from ldr import processes
    (tmp_path / 'app.py').write_text('def f(x): return x + 1\nf(2)\n')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    def run():
        return processes.run([sys.executable, '-m', 'ldr', 'run', 'app.py'], cwd=tmp_path,
                             env=env, capture_output=True, text=True, check=True)
    run()
    db = sqlite3.connect(tmp_path / '.ldr' / 'traces.sqlite3')
    run_id, raw = db.execute('SELECT id,invocation FROM runs').fetchone()
    descriptor = json.loads(raw)
    descriptor.pop('signature_version')
    db.execute('UPDATE runs SET invocation=? WHERE id=?', (json.dumps(descriptor), run_id))
    db.commit()
    second = run()
    assert 'signature format v1 is incompatible with v2' in second.stdout
    third = run()
    assert 'signature format v1 is incompatible' not in third.stdout
    payload = json.loads((tmp_path / '.ldr' / 'last.json').read_text())
    assert payload['status']['reference']['compared'] > 0


def test_added_real_slot_contents_do_not_change_v1_node_accounting():
    class Slots:
        __slots__ = ['item']
    value = Slots()
    value.item = [1, 2]
    assert encoded(value)[1] == 4
    Slots.__slots__.clear()
    # v1 reads the edited declaration and visits the root alone. v2 still hashes
    # the real member descriptor, but must retain v1's accounting.
    assert encoded(value)[1] == 1


def test_all_value_cache_fixtures_detect_changes_or_redaction():
    from test_value_cache import values
    for value in values():
        before = fp.fingerprint(value)
        if before == fp.REDACTED:
            continue
        if type(value) is bool:
            changed = not value
        elif type(value) is int:
            changed = value + 1
        elif type(value) is float:
            changed = 0.0 if math.isnan(value) or math.isinf(value) else value + 1.0
        elif type(value) is str:
            changed = 'changed-' + value
        elif type(value) is tuple:
            changed = value + (None,)
        else:
            changed = 1 if value is None else None
        after = fp.fingerprint(changed)
        if type(value) is tuple:
            for index, item in enumerate(value):
                replacement = 1 if item is None else None
                altered = value[:index] + (replacement,) + value[index + 1:]
                assert fp.fingerprint(altered) != before
        # The temp-path normalizer deliberately hides components under the temp directory.
        if type(value) is str and fp.normalize_temp_paths(value) != value:
            continue
        assert after != before


def test_unreadable_builtin_remains_unsupported():
    import re
    assert fp.fingerprint(re.compile('example')) == fp.UNSUPPORTED
    assert fp.fingerprint(re.compile('example'), partial_ok=True).endswith('+')


def test_each_readable_rich_field_changes_the_signature():
    import copy
    from ldr.structure import read
    from rich.console import Console
    from rich.segment import Segment
    from rich.style import Style
    from rich.text import Text
    from rich.theme import Theme, ThemeStack

    values = [ThemeStack(Theme({'x': Style(color='red')})), Segment('text', Style(bold=True)),
              Style(color='red'), Text('text'), Console(width=40)]
    for value in values:
        before = fp.fingerprint(value, partial_ok=True)
        assert before not in fp.STATUSES
        for name, field in read(value).fields:
            if name is None:
                continue
            if field is None:
                replacement = 'changed'
            elif type(field) is bool:
                replacement = not field
            elif type(field) is int:
                replacement = field + 1
            elif type(field) is str:
                replacement = field + '-changed'
            elif type(field) is list:
                replacement = [*field, 'changed']
            else:
                replacement = None
            if isinstance(value, Segment):
                changed = value._replace(**{name: replacement})
            else:
                changed = copy.copy(value)
                setattr(changed, name, replacement)
            assert fp.fingerprint(changed, partial_ok=True) != before, (type(value).__name__, name)


def test_pytest_run_reports_previous_v1_mismatch(tmp_path):
    import json, os, sqlite3, sys
    from pathlib import Path
    from ldr import processes
    (tmp_path / 'test_example.py').write_text('def test_example():\n    assert 1 + 1 == 2\n')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    def run():
        return processes.run([sys.executable, '-m', 'ldr', 'pytest', 'test_example.py'], cwd=tmp_path,
                             env=env, capture_output=True, text=True, check=True)
    run()
    db = sqlite3.connect(tmp_path / '.ldr' / 'traces.sqlite3')
    run_id, raw = db.execute('SELECT id,invocation FROM runs').fetchone()
    descriptor = json.loads(raw)
    descriptor.pop('signature_version')
    db.execute('UPDATE runs SET invocation=? WHERE id=?', (json.dumps(descriptor), run_id))
    db.commit()
    result = run()
    assert 'signature format v1 is incompatible with v2' in result.stdout
    status = json.loads((tmp_path / '.ldr' / 'last.json').read_text())['status']
    assert not status.get('reference')
    assert status['previous_unavailable'] == 'signature format v1 is incompatible with v2'
