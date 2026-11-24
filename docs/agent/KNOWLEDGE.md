# LDR Knowledge Folder

The repository does not hold LDR's durable context. It lives in the LDR
knowledge folder on Aymen's Google Drive, in the cloud only. Nothing is
synced to a disk.

## Location

- Folder `LDR` at the root of Aymen's My Drive. Durable context is in
  `LDR/knowledge/`; `LDR/START-HERE.md` explains the folder.
- Reach it through your Google Drive connector. Find files by name.
- There is no local path and no environment variable for it.

## Read rules

- Read `LDR/START-HERE.md` completely at every session start and follow its
  read order.
- If the connector is missing, or a required file cannot be read, report a
  degraded start and stop knowledge-dependent work. Never use an old copy,
  an upload, a file pasted earlier or platform memory instead.
- Report the `LE-TOPO.md` "Last updated" date in the session summary, so a
  stale handoff is visible.
- Do not recite the knowledge back unless a conflict or Aymen's request makes
  it useful.

## Write rules

Normal work never edits the knowledge folder.

To edit a Drive file, start from a raw download; never write back the normal read, which adds Markdown escapes.

Only when Aymen explicitly asks for a knowledge update during `wrap`:

- You may rewrite `LE-TOPO.md` and `phase-handoff.md`, update the status
  column of `BATCHES.md` and add rows to `MEASUREMENTS.md`.
- You may not edit `PROJECT.md`, `DESIGN-TARGET.md`, `DECISIONS.md`,
  `LATER.md`, `design-v1.md`, `later-full.md` or the batch definitions in
  `BATCHES.md`, unless a batch explicitly asks. Those belong to the
  architect. Propose changes to them in your report.
- Every file is plain Markdown: upload as `text/markdown` with conversion to
  Google Docs disabled. Never create a Google Doc.
- To change a file, follow "Updating a file" in `LDR/START-HERE.md`: read it
  right before writing, upload the new version with the same name in the
  same folder, read it back, then trash the old one. Exactly one file per
  name.
- Report every knowledge file you changed.

Only one agent writes to the knowledge folder at a time. Aymen decides who.
