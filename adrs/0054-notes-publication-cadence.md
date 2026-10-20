---
id: 54
title: Notes publication cadence and freshness
status: accepted
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0054: Notes publication cadence and freshness

## Context

[[adrs/0034-automatic-notes-sync|ADR-0034]] syncs the notes repo from hooks. Every Stop published usage,
so usage commits piled up, several within seconds of each other, and `notes-sync` took a large
share of summed hook time. A session whose client pid could not be
proven pulled on every prompt. Cross-machine evidence also needs the publisher to push before it
tells the reader, and the reader to have that commit before acting.

## Decision

Notes publication is rate-limited and freshness is explicit. A turn's Stop publishes usage at
most once per 30 minutes, and usage writes no file whose figures are unchanged. A prompt from a
session with an unproven client pid skips its pull within 120 s of a successful one. A prompt or
unread inbox row naming `notes-commit: <sha>` that is not an ancestor of the checked-out HEAD
always pulls, and a commit still missing afterwards is reported. The `harness` tool's
`notes_publish` action commits and pushes now and names the verified commit.

## Consequences

- Dispatch completion and session start still publish usage at once; nothing collected is lost.
- Evidence moves as a published notes file plus its `notes-commit:`, never a paste.
- Lines amended in ADR-0034: "with `notes-sync` on UserPromptSubmit as the fallback" now carries
  the 120 s window and the named-commit pull. "Files a render regenerates are not tracked": doctor
  no longer checks the notes `.gitignore` and `.gitattributes`; sync appends missing lines to
  existing files before it stages.

## Alternatives considered

- A new publication queue: `usage/job.py` already coalesces runs; only the trigger rate changed.
- Pulling on every prompt: it costs a fetch per machine wake for no new data.
