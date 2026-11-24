# Command: trace

Reconstruct one LDR decision, behavior or change chain without editing
anything.

1. Make sure the knowledge folder and the repository are readable.
2. Read only what the question needs: `DECISIONS.md`, `BATCHES.md`, the
   relevant part of `design-v1.md`, the code, and `git log` / `git show` for
   the commits involved.
3. Do not load or summarize unrelated history.
4. Separate:
   - What a document states.
   - What the code or Git history shows.
   - What you infer.
   - What is unknown.
5. If sources conflict, show the conflict and what it changes today.

Return a short chronological explanation with exact file and commit
references, ending with the consequence for current work.
