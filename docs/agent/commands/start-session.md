# Command: start-session

Start or resume an LDR session without editing files or starting work.

## Load

1. Read `docs/AGENT-CORE.md`.
2. Read `docs/agent/KNOWLEDGE.md`. Reach the knowledge folder through your
   Google Drive connector (folder `LDR` at the root of My Drive).
3. Read `LDR/START-HERE.md` completely and follow its read order:
   `PROJECT.md`, `LE-TOPO.md`, `BATCHES.md`, `DESIGN-TARGET.md`,
   `DECISIONS.md`.
4. Read `MEASUREMENTS.md` only if the focus involves speed, size or cost.
5. Check the current branch, the working tree, and how the local branch
   compares with its remote (ahead, behind, diverged).
6. Read only the code the current batch or the requested focus touches.

Do not edit files, change status, commit or start implementation.

If the Drive connector is missing or the knowledge folder cannot be read,
report a degraded start and stop.
Do not inspect extra code as a substitute.

## Report

Keep it short:

- Branch, working tree, ahead/behind.
- `LE-TOPO.md` "Last updated" date.
- The current batch and its status.
- What is done, what is in progress, what is blocked.
- Any conflict between the knowledge files and the code you read.
- The next useful move.

Requested focus: use the text given with the command, if any.
