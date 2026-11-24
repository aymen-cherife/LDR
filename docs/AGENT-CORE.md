# LDR Agent Core

This is the shared operating system for coding agents working in the LDR
repository. It is the same authority for Codex and Claude Code.

## Project

LDR is a Python runtime-evidence tool for coding agents. It records a run,
lets the agent read the evidence, and compares the run with the previous run
of the same target. Its users are coding agents, not humans: everything it
prints must be short, exact and end with the next useful command.

Durable context (design target, decisions, batch status, handoff,
measurements) lives outside the repository, in the LDR knowledge folder.
Read `docs/agent/KNOWLEDGE.md` for how to reach it.

## Roles and authority

- **Aymen** owns LDR. He has the final say on design, scope, names and
  accepted tradeoffs. Names are working names until he says they are final.
- **The architect** (Claude, in claude.ai) writes the batch prompts and checks
  the reports. A batch prompt pasted by Aymen is an approved target.
- **The coding agent** (you) executes one approved batch, reports and stops.
  You are responsible for honest technical judgment: challenge a weak step
  before coding it, not after.

Stop and ask before acting when:

- The real code differs materially from what the batch assumes.
- A step would add anything new: a command, flag, file, cap, limit, setting,
  dependency or output section the design does not already name.
- A step conflicts with `DESIGN-TARGET.md` or `DECISIONS.md` in the knowledge
  folder.
- The batch is ambiguous or required information is missing.
- A decision cannot be undone.

Asking costs a minute. A wrong guess costs a batch.

## Sources of truth

1. Real code and tests describe what exists.
2. Aymen's latest explicit decision.
3. The approved batch prompt.
4. Knowledge `DESIGN-TARGET.md` and `DECISIONS.md` describe what is decided.
5. Knowledge `design-v1.md` (on Drive) is the long form of the design.
6. Knowledge `BATCHES.md` holds status; `LE-TOPO.md` holds the handoff.

If sources conflict materially, expose the conflict. Never choose silently
and never average.

## Hard rules of the product

Check every change against these. A change that breaks one is wrong even if
its tests pass.

- **No silent cuts.** Every list, value or report that is shortened states how
  many items were dropped and the exact command to see them.
- **No arbitrary caps.** Do not add a size, count or step limit. The only
  limit that stays is the 20,000-node walk ceiling, a safety limit for the
  comparison only. Do not lower it. Kept detail stays full.
- **Nothing of value is dropped for speed.** LDR runs when a problem exists.
  Make the work cheaper; do not record less.
- **Never mask silently.** No timestamp masking, no ignore lists, no hidden
  noise. Every change is shown; noise is labelled, not hidden.
- **No fixed thresholds or timers that block, refuse, gate or hide.** Report
  the measured fact. A problem is only an objective condition (a process is
  gone, a file is locked, the disk cannot hold the next run). Never lower a
  bar to make something pass.
- **No database question on the hot path.** Anything read while the program
  records is loaded into memory first. Writes go out in batches.
- **Every run is announced**, including the rerun `ldr inspect` starts.
- **The program's exit code passes through unchanged.**
- **Command lines and environment never touch disk.** Assume every byte LDR
  prints goes to a model provider.
- **Fits how agent teams work:** usable as a step in a verify skill, by hooks
  and by `/goal`; short output; progress live, never silent.

## Engineering rules

- Read the real code before deciding how to change it.
- Failing tests first where it applies: write the test, see it fail, then fix.
- Prefer deleting code to wrapping it. No compatibility layers for removed
  commands or flags.
- Names say what the thing concretely does. No `*_manager`, `*_handler`,
  `utils`. When a name changes, sweep every reference in the same pass: code,
  tests, output text, settings, docs.
- Delete one-off experiment scripts once their question is answered and the
  finding is recorded (commit message or knowledge `MEASUREMENTS.md`).
- No speculative abstractions. No unrelated optimizations. Propose them in
  the report with expected benefit, cost and tradeoff.
- Windows and Linux both matter. Aymen develops on Windows.
- Every process LDR, its harness or its tests start goes through the shared
  helper in `ldr/processes.py` (no console window on Windows). A test
  enforces it.
- Do not call `sys.monitoring.restart_events()` globally.

## Measurements

Any timing, size or cost number follows `docs/agent/commands/measure.md`:
single runs, the cost of a series stated before it starts, conditions with
every number. A number without its conditions is not reported.

## Verification

Always separate:

- Tests actually run (exact command and result).
- Behavior actually observed.
- What is only inferred from reading code.

Run the tests the change touches while working. At the end of any batch that
changes code, run the full suite once, alone, with nothing else running
(about 7 to 10 minutes on Aymen's machine as of 2026-11-24).

## Git, safety and secrets

- Check the branch and working tree before editing. Preserve unrelated changes.
- Commit author email: `153111060+aymen-cherife@users.noreply.github.com`
  only. Never another email.
- One commit per logical step, message in plain words saying what changed and
  why.
- Commit when the batch says so. Push only when the batch or Aymen says so.
  Never force-push.
- `docs/design-v1.md`, `docs/later.md` and `docs/phase-handoff.md` are
  frozen local files (in `.git/info/exclude`). Their live copies are in the
  Drive knowledge folder. Never edit or commit the local ones.
- Never print, write or commit secrets, tokens or the contents of a user's
  environment.

## Repository commands

- `start-session`: load knowledge, Git state and the current batch without
  starting work.
- `pre-exec-check`: restate the approved batch for Aymen's confirmation.
- `execute`: implement the approved batch, then report and stop.
- `measure`: take timing, size or cost numbers the right way.
- `review-change`: read-only review of a change against the design.
- `trace`: reconstruct one decision or behavior chain.
- `wrap`: reconcile state and, when asked, update the knowledge folder.
