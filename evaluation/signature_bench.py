"""Compare Python and native v2 walks on live references retained from the real Rich nine-file run.

Run directly with Python. A normal recording supplies per-test outcomes; a second
recording retains every actual walk input and benchmarks those references after
pytest returns, in that same interpreter. No value cache is used in the replay.
"""
from __future__ import annotations

import collections
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter, perf_counter_ns

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from ldr import fingerprint as fp, processes


class Samples:
    def __init__(self):
        self.values = []

    def capture(self, value, budget, partial_ok):
        # Keep each occurrence, including repeats of the same object. No copies.
        self.values.append((value, budget, partial_ok))

    def measure(self):
        by_type = collections.defaultdict(lambda: [0, 0, 0])
        statuses = collections.Counter()
        total_ns = nodes = 0
        for value, budget, partial_ok in self.values:
            label = fp.type_identity(type(value))
            walk = fp._Walk(budget, partial_ok)
            output = hashlib.blake2b(digest_size=fp.DIGEST_BYTES)
            started = perf_counter_ns()
            try:
                walk.encode(value, output)
                output.digest()
                status = 'complete'
            except fp._Stop as error:
                status = error.status
            except RecursionError:
                status = fp.UNKNOWN
            except Exception:
                status = fp.FAILED
            elapsed = perf_counter_ns() - started
            total_ns += elapsed
            nodes += walk.visited
            row = by_type[label]
            row[0] += 1
            row[1] += walk.visited
            row[2] += elapsed
            statuses[status] += 1
        top = sorted(by_type.items(), key=lambda pair: pair[1][2], reverse=True)[:10]
        return {'reader': 'python-v2', 'values': len(self.values), 'nodes': nodes,
                'seconds': total_ns / 1e9, 'ns_per_node': total_ns / nodes if nodes else None,
                'statuses': dict(statuses), 'top_types': [
                    {'type': name, 'values': row[0], 'nodes': row[1], 'seconds': row[2] / 1e9}
                    for name, row in top], 'peak_rss_bytes': peak_memory()}


    def compare(self, reader):
        """Check wire parity separately, then time each reader once on these references.

        The validation pass warms both class-layout caches. It is not included
        in either timing; no value is copied or reconstructed between passes.
        """
        for index, (value, budget, partial_ok) in enumerate(self.values):
            expected = python_wire(value, budget, partial_ok)
            actual = reader.walk(value, budget=budget, partial_ok=partial_ok)
            for key in ('bytes', 'visited', 'status', 'partial', 'immutable', 'normalized'):
                if actual[key] != expected[key]:
                    raise AssertionError(f'wire parity failed at value {index}, '
                                         f'{fp.type_identity(type(value))}, field {key}; '
                                         f'Python nodes={expected["visited"]}, native nodes={actual["visited"]}')
            if actual['digest'] != hashlib.blake2b(expected['bytes'], digest_size=16).hexdigest():
                raise AssertionError(f'digest parity failed at value {index}')
            if not 0 <= actual['delegated'] <= actual['visited']:
                raise AssertionError(f'invalid delegation count at value {index}')
        return {'parity_values': len(self.values), 'python': self.measure(),
                'native': self.measure_native(reader)}

    def measure_native(self, reader):
        by_type = collections.defaultdict(lambda: [0, 0, 0])
        statuses = collections.Counter()
        total_ns = nodes = delegated = 0
        for value, budget, partial_ok in self.values:
            label = fp.type_identity(type(value))
            reader.refresh()  # policy preparation is outside the walk timer
            core = reader.core
            normalized_before = fp.STATS.tmp_normalized
            started = perf_counter_ns()
            result = core.walk(value, partial_ok)
            elapsed = perf_counter_ns() - started
            fp.STATS.tmp_normalized = normalized_before
            total_ns += elapsed
            nodes += result['visited']
            delegated += result['delegated']
            row = by_type[label]
            row[0] += 1
            row[1] += result['visited']
            row[2] += elapsed
            statuses[result['status']] += 1
        top = sorted(by_type.items(), key=lambda pair: pair[1][2], reverse=True)[:10]
        return {'reader': 'native-v2', 'values': len(self.values), 'nodes': nodes,
                'seconds': total_ns / 1e9, 'ns_per_node': total_ns / nodes if nodes else None,
                'delegated_nodes': delegated, 'native_nodes': nodes-delegated,
                'native_share': (nodes-delegated)/nodes if nodes else None,
                'statuses': dict(statuses), 'top_types': [
                    {'type': name, 'values': row[0], 'nodes': row[1], 'seconds': row[2]/1e9}
                    for name, row in top], 'peak_rss_bytes': peak_memory()}


class Wire:
    def __init__(self):
        self.data = bytearray()
    def update(self, data):
        self.data.extend(data)


def python_wire(value, budget, partial_ok):
    output = Wire()
    walk = fp._Walk(budget, partial_ok)
    normalized = fp.STATS.tmp_normalized
    status = 'complete'
    try:
        walk.encode(value, output)
    except fp._Stop as error:
        status = error.status
    except RecursionError:
        status = fp.UNKNOWN
    except Exception:
        status = fp.FAILED
    finally:
        normalized_count = fp.STATS.tmp_normalized-normalized
        fp.STATS.tmp_normalized = normalized
    return {'bytes': output.data, 'visited': walk.visited, 'status': status,
            'partial': walk.partial, 'immutable': walk.immutable, 'normalized': normalized_count}


def native_reader():
    from ldr.native_bridge import Reader
    module = importlib.import_module('ldr._native')
    if not hasattr(module, 'ReaderCore'):
        # Development builds stay out of the Python recorder until runtime
        # selection is implemented. Load the per-interpreter prototype here.
        binary = REPO / 'ldr' / '_native' / 'target' / f'py{sys.version_info.major}{sys.version_info.minor}' / 'release' / 'lib_native.so'
        if not binary.exists():
            raise RuntimeError('native module not built')
        spec = importlib.util.spec_from_file_location('ldr._native', binary)
        module = importlib.util.module_from_spec(spec)
        sys.modules['ldr._native'] = module
        spec.loader.exec_module(module)
    return Reader(module)


def peak_memory():
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == 'darwin' else rss * 1024


def result_differences(normal, kept):
    return [{'position': key, 'normal': normal.get(key), 'kept': kept.get(key)}
            for key in sorted(normal.keys() | kept.keys()) if normal.get(key) != kept.get(key)]


class Results:
    def __init__(self):
        self.outcomes = {}

    def pytest_runtest_logreport(self, report):
        outcome = report.outcome
        if hasattr(report, 'wasxfail'):
            outcome += ':xfail'
        self.outcomes[report.nodeid + ':' + report.when] = outcome

    def pytest_collectreport(self, report):
        if report.failed:
            self.outcomes[report.nodeid + ':collection'] = report.outcome


def install(output_path, keep):
    """Called by a temporary sitecustomize only in LDR's program process."""
    import pytest
    samples = Samples()
    original_encode, original_main = fp._Walk.encode, pytest.main

    def captured(walk, value, output):
        samples.capture(value, walk.budget, walk.partial_ok)
        return original_encode(walk, value, output)

    if keep:
        fp._Walk.encode = captured

    def main(*args, **kwargs):
        results = Results()
        plugins = list(kwargs.get('plugins', ()))
        kwargs['plugins'] = [*plugins, results]
        try:
            code = original_main(*args, **kwargs)
        finally:
            fp._Walk.encode = original_encode
            pytest.main = original_main
        # pytest has completed all test teardown. Stop recording before timing.
        for plugin in plugins:
            recorder = getattr(plugin, 'recorder', None)
            if recorder is not None:
                recorder.stop()
        row = {'outcomes': results.outcomes, 'exit_code': int(code)}
        if keep:
            normalized_before = fp.STATS.tmp_normalized
            try:
                comparison = samples.compare(native_reader())
                row['benchmark'] = comparison['python']
                row['native_benchmark'] = comparison['native']
                row['parity_values'] = comparison['parity_values']
            finally:
                fp.STATS.tmp_normalized = normalized_before
        Path(output_path).write_text(json.dumps(row), encoding='utf8')
        return code

    pytest.main = main


def main():
    import overhead
    work = Path(tempfile.gettempdir()) / 'ldr-c2b'
    root, _, traced = overhead.build(work, 'rich')
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix='ldr-signature-bench-') as scratch:
        scratch = Path(scratch)
        rows = []
        for label, keep in [('normal', False), ('retained', True)]:
            output = scratch / (label + '.json')
            hook = scratch / label
            hook.mkdir()
            hook.joinpath('sitecustomize.py').write_text(
                "import os\nif os.environ.get('LDR_RUN_CHILD') == '1':\n"
                f"    import signature_bench\n    signature_bench.install({str(output)!r}, {keep!r})\n",
                encoding='utf8')
            env = overhead.environment(REPO, root)
            env['PYTHONHASHSEED'] = '0'
            env['PYTHONPATH'] = str(hook) + os.pathsep + env['PYTHONPATH']
            overhead.clean(root)
            begin = perf_counter()
            result = processes.run(traced, cwd=root, env=env, capture_output=True, text=True)
            if not output.exists():
                raise RuntimeError(f'{label} did not produce results (exit {result.returncode})\n'
                                   + result.stdout + result.stderr)
            row = json.loads(output.read_text(encoding='utf8'))
            row['recording_wall_seconds'] = perf_counter() - begin
            rows.append(row)
        differences = result_differences(rows[0]['outcomes'], rows[1]['outcomes'])
        print(json.dumps({'fixture': 'rich-15.0.0-nine-files', 'pythonhashseed': '0',
                          'python': sys.version.split()[0], 'platform': sys.platform,
                          'normal_exit': rows[0]['exit_code'], 'retained_exit': rows[1]['exit_code'],
                          'result_differences': differences,
                          'normal_wall_seconds': rows[0]['recording_wall_seconds'],
                          'retained_wall_seconds': rows[1]['recording_wall_seconds'],
                          'benchmark': rows[1]['benchmark'],
                          'native_benchmark': rows[1]['native_benchmark'],
                          'parity_values': rows[1]['parity_values'],
                          'total_wall_seconds': perf_counter() - started}), flush=True)
        return int(any(row['exit_code'] for row in rows) or bool(differences))


if __name__ == '__main__':
    raise SystemExit(main())
