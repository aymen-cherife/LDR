# LDR agent evaluation

This harness compares the same coding-agent configuration with and without LDR. It measures the complete agent configuration—not the model name alone.

`tasks.json` contains eight recent SWE-bench-Live Python tasks selected for runtime-diagnostic value. Prepare two clean checkouts of the same task, including the benchmark test patch and dependencies. Install the current LDR checkout only in the assisted environment.

```bash
python evaluation/prepare_swebench_live.py HypothesisWorks__hypothesis-4532 \
  --workspace /tmp/hypothesis-ldr \
  --task-json /tmp/hypothesis-4532.json
```

The preparation command never overwrites a path. It checks out the benchmark base commit, applies and locally commits the public test patch, and leaves dependency installation to the repository or benchmark environment.

Run the same headless agent command and budget in each checkout:

```bash
python evaluation/run_agent.py \
  --workspace /tmp/task-baseline \
  --task-json /tmp/task.json \
  --condition baseline \
  --command 'claude -p --output-format stream-json'

python evaluation/run_agent.py \
  --workspace /tmp/task-ldr \
  --task-json /tmp/task.json \
  --condition ldr \
  --command 'claude -p --output-format stream-json'
```

Use clean sessions and randomize condition order. Do not share trajectories, patches, or LDR output across conditions. Run each task at least twice when cost allows.

Primary metric: gold evaluation tests resolved. Secondary metrics: wall time, observed tokens, changed files, patch size, test reruns, LDR call rate, and whether LDR identified the gold-patch function before the first application-source edit.

Internal continuation threshold: at least two additional tasks solved out of eight, or at least 25% less investigation cost without a lower solve rate. This is a development gate, not a publishable statistical claim.
