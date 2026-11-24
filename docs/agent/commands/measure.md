# Command: measure

Take timing, size or cost numbers for LDR. Numbers are directional: they
show which way and roughly how much, to guide development. Used on its own
or inside `execute`.

## Before

1. State the cost up front, before running anything: the runs planned, the
   time each takes, and the total. Measure only what the change can affect,
   on the fewest fixtures that show it.
2. Run `ldr check_machine` and record its output with the results. Never wait
   on it and never refuse to run because of it.
3. Nothing else heavy runs during a series: no tests, builds or other agents
   in parallel. Kill any background job you started once it is done.
4. State the conditions: machine and OS, Python version, commit, fixture or
   target, LDR mode (plain = no LDR, default, `--full`, ...).

## How

- Single runs. No repeats, no medians.
- Report the phases a run records (python, start, program, compare, writing,
  pruning, return), not only the wall time. A phase without its own timer is
  a remainder: name it as such, do not label it as if it were measured.
- "Before vs after" in the same session when that comparison is the point,
  by checking out the old commit. A number from an earlier session may be
  shown for context, labelled with its session.
- Per-call cost = (traced time - plain time) / calls recorded.
- A stand-in fixture must match the real project before its numbers count:
  same object types and sizes (including slotted objects), same mix of roles
  (receiver, argument, return), same share of calls that keep full detail.
- If a run behaves abnormally, find the cause before rerunning. Never retry
  blindly.
- Never add a fixed threshold, timer or wait to a harness.

## Report

- A table: configuration, phases, wall time.
- The conditions and the `check_machine` output.
- What the numbers say, in one or two plain sentences.
- Anything that makes them less reliable.
- Label every extrapolation "unmeasured".

Add durable results to knowledge `MEASUREMENTS.md` only during `wrap`, when
Aymen asks for a knowledge update.

Delete one-off measurement scripts and their data when done, unless the
batch keeps them (for example under `evaluation/`).
