# Command: review-change

Read-only review of an LDR change against the batch, the design and the hard
rules. Do not edit, fix, commit or change status.

1. Make sure `start-session` was done in this session.
2. Identify the exact change: commits or diff, and the batch it belongs to.
3. Read the batch prompt, `DESIGN-TARGET.md`, `DECISIONS.md`, the diff and
   the surrounding code.
4. Check, at least:
   - Does it do what the batch asked, no more and no less?
   - Hard rules: a silent cut, a new cap or limit, anything dropped for
     speed, silent masking, an unannounced run, a changed exit code, a command
     line or environment written to disk.
   - Something new (command, flag, file, setting, output section) that was
     never approved.
   - A removed command or flag kept alive through a compatibility path.
   - Names that drift from the design's names, or a rename not swept
     everywhere.
   - Windows and Linux behavior.
   - Tests: do they test the behavior, or only that code runs?
   - Numbers: conditions stated, same session, repeats and spread.
5. Do not widen this into an audit of the whole repository.

## Report

Findings in order of severity. For each:

- Severity.
- File and line.
- What is wrong.
- Why it matters for an agent using LDR.
- The smallest sound fix.

Then:

- Batch steps done, partly done, not done.
- Checks actually evidenced.
- What still needs running or observing.

If there are no material findings, say so. Do not invent issues.
