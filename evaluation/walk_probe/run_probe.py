"""One recorded run of an overhead fixture with the walk probe (see sitecustomize.py).

    python evaluation/walk_probe/run_probe.py NAME OUT_PREFIX [--work DIR] [--ldr CHECKOUT]

NAME is a fixture of evaluation/overhead.py. The probe writes OUT_PREFIX.<pid>; read it with
evaluation/repeated_walks.py. The run is slower than a plain `ldr` run (the probe walks sampled
values a second time, and keeps every walked object alive so that an id is never reused).
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import overhead  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("name")
parser.add_argument("out")
parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "ldr-overhead"))
parser.add_argument("--ldr", default=str(overhead.REPO))
arguments = parser.parse_args()
root, plain, traced = overhead.build(Path(arguments.work), arguments.name)
env = overhead.environment(Path(arguments.ldr), root)
env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + str(HERE)
env["LDR_PROBE_OUT"] = arguments.out
overhead.clean(root)
started = time.perf_counter()
overhead.timed(traced, root, env)
print("traced seconds", round(time.perf_counter() - started, 2), "root", root)
