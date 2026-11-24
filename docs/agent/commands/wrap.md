# Command: wrap

Reconcile the work done in this session with reality. Do not assume work is
done because the session is ending.

## Inspect first

1. Git status, the commits made this session, unpushed commits and the
   uncommitted diff.
2. Which batch steps were done, partly done, not started.
3. Which checks and measurements were actually run.

## Report

- Commits, and whether pushed.
- Batch steps and their state.
- Uncommitted changes and what they are.
- Checks run, and what still needs running or observing.
- Problems, uncertainty, blockers.
- The next useful move.

## Knowledge update (only when Aymen asks)

Normal `wrap` does not edit the knowledge folder. When Aymen explicitly asks
for a knowledge update, follow the write rules in `docs/agent/KNOWLEDGE.md`:

1. Re-read the files right before editing.
2. Rewrite `LE-TOPO.md` around current reality, not as a diary. Keep:
   "Last updated" date, branch and commit, current batch, what changed,
   important reasoning, checks done, problems and open items, next useful
   move, files to load next.
3. Update the status column of `BATCHES.md`: `todo`, `executing`, `paused`
   (with the reason and what unblocks it), `done` (with the commits), or
   `dropped` (with the reason). Only Aymen drops a batch.
4. Add durable numbers to `MEASUREMENTS.md`, each with its date, commit and
   conditions.
5. Do not edit architect-owned files. List proposed changes to them in the
   report.
6. Report every knowledge file you changed.

Do not commit or push unless asked.
