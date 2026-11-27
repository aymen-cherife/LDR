"""Time Python v2 walks on live references retained from the real Rich nine-file run.

Run directly with Python. A normal recording supplies per-test outcomes; a second
recording retains every actual walk input and benchmarks those references after
pytest returns, in that same interpreter. No value cache is used in the replay.
"""
from __future__ import annotations

import collections
import hashlib
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
                row['benchmark'] = samples.measure()
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
                          'total_wall_seconds': perf_counter() - started}), flush=True)
        return int(any(row['exit_code'] for row in rows) or bool(differences))


if __name__ == '__main__':
    raise SystemExit(main())
