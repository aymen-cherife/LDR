# Command: pre-exec-check

Restate the approved batch in your own words so Aymen can confirm you
understood it before any code changes.

This is a read-only understanding pass, not a new discovery or audit stage.

## Ground it

1. Make sure `start-session` was done in this session.
2. Use the batch prompt Aymen gave, his latest decisions in the
   conversation, and the knowledge files already loaded.
3. Prefer the latest explicit decision. Leave out rejected ideas and options
   nobody accepted.
4. Check the batch against `DESIGN-TARGET.md` and `DECISIONS.md`. If a step
   contradicts them, or adds something new the design does not name, say so.
5. Do not hunt for new uncertainty. Ask only about a known gap that blocks
   the batch.

## Report

**What I understood**: one short paragraph, in your own words.

**I will**: three to seven bullets, one per step of the batch, each saying
the intended result.

**I will not touch**: the important boundaries (what stays unchanged, what is
out of scope).

**How I will check it**: the tests and measurements the batch expects.

**Conflicts or questions**: only if something blocks confirmation.

Keep it between 150 and 300 words. No code, no detailed plan.

End with one of:

`Ready. I will wait for "confirmed, execute".`

`Not ready: I need the decision above.`

This command never authorizes execution by itself.
