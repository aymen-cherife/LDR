# Command: execute

Implement one approved LDR batch, report, and stop.

## Resolve the target

1. Make sure `start-session` was done in this session.
2. The target is the batch prompt Aymen gave with this command. If he ran
   `pre-exec-check`, wait for his "confirmed, execute" or an equally explicit
   approval. The check's own summary is not approval.
3. Read the code each step touches and verify the batch's starting
   assumptions. If the code differs materially, stop and ask before editing.
4. Check the branch and working tree. Preserve unrelated changes.

## Implement

- Do the steps in the order given. Do only what the batch says.
- Failing tests first where it applies.
- Follow the hard rules and engineering rules in `docs/AGENT-CORE.md`.
- Anything new (command, flag, file, cap, limit, setting, dependency, output
  section) that the batch does not name: stop and ask first.
- If a step cannot be done as written, say why, do the rest, and report it.
  Do not quietly substitute something else.
- If you add something extra that helps (for example a second fixture),
  label it as extra in the report and keep the requested version as the one
  the batch's checks are judged on.
- Timing, size or cost numbers: follow `docs/agent/commands/measure.md`.
- Do not run the full test suite unless the batch asks.
- Commit per logical step when the batch says to commit. Push only when the
  batch or Aymen says so.

## Report

When the batch is done, report in this order and stop:

1. **Commits**: hash and one line each, and whether pushed.
2. **Per step**: what was done, in a few lines. Numbers with their conditions.
3. **Checks run**: exact commands and results.
4. **Not done as written**: what, and why.
5. **Extras**: anything added beyond the batch, labelled.
6. **Noticed, not acted on**: bugs, risks or ideas found along the way.
7. **Questions** for the architect or Aymen, if any.

Separate what was measured or observed from what is inferred from code.
Keep the report short; the architect checks it line by line.

Do not start the next batch.
