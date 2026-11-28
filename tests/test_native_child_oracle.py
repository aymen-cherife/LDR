"""The fingerprint-test subprocesses must take part in Gate 1 too."""
import json
import os
import sys
from pathlib import Path
import pytest
from ldr import processes
from test_native_v2 import native


def test_subprocess_oracle_records_each_real_walk(tmp_path):
    native()
    import native_child_oracle
    hook=tmp_path/'hook';hook.mkdir()
    output=tmp_path/'results';output.mkdir()
    (hook/'sitecustomize.py').write_text(f'from native_child_oracle import install\ninstall({str(output)!r})\n')
    repo=Path(__file__).parents[1]
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(map(str,(hook,repo,repo/'tests'))))
    result=processes.run([sys.executable,'-c',
        'from ldr import fingerprint as fp;fp.fingerprint([1,2]);fp.fingerprint({"a", "b"})'],
        env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    reports=[json.loads(path.read_text()) for path in output.glob('*.json')]
    assert sum(row['values'] for row in reports)==2
    assert all(not row['failures'] for row in reports)
