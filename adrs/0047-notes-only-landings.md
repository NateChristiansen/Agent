---
id: 47
title: A notes-only change lands on scoped tests, chained to a full pass record
status: superseded
date: 2026-10-07
supersedes: null
superseded_by: 62
tags:
- adr
links: []
---

# ADR-0047: A notes-only change lands on scoped tests, chained to a full pass record

Amends ADR-0041 (what certifies a landed tree) and ADR-0045 (the landing certificate is the one
full run).

## Context

A landing that changed only `harness/lessons/queue.md` ran the full suite for a Markdown edit
that no product code reads. A lessons commit that waits for a code landing to ride along leaves
the main checkout dirty in the meantime.

## Decision

`harness/notes-only.toml` lists the notes-only paths (`harness/lessons/**` to start) and the tests
that read them. `agent land` certifies a candidate with a **notes-only record** instead of a full
pass record when all of these hold, checked under the land lock:

1. Current main is **anchored**: it has an applicable full pass record itself, or a valid
   notes-only record whose anchor does. Anything missing, unreadable or inconsistent means a full
   run.
2. The whole diff from the anchor to the candidate (earlier notes-only landings and every batch
   member included) touches only the listed paths, read from the **anchor's** tree, never the
   candidate's. A diff that edits the policy file itself always gets the full suite.
3. The verify commands pass with each pytest command narrowed to the listed tests, in the same
   isolated checkout and lane as a full run.

The notes-only record is stored apart from pass records (`.agent/verify/notes-only/<tree>.json`)
with its anchor, its scoped run and the paths. It never becomes or counts as a full pass record.
Every reader recomputes the classification from git and re-checks the anchor's full record and
the scoped run's evidence hashes; the stored record is only a pointer, so a tampered one fails
closed. A landing whose main moved after classification is refused by the landing transaction.

Publication: the pre-push gate accepts a pushed tip that is fully certified, or anchored this way.
A chain with a code change anywhere in it is not notes-only from the anchor and needs a full run.
Successive notes-only landings each chain to the same anchor until a full run lands.

## Consequences

- A lessons-only landing costs the lint and type commands plus `tests/test_lessons.py`, not the
  suite.
- Widening the path list is itself a full-suite change, reviewed like any other.
- Doctor's and session start's "main has no pass record" accept an anchored tip.
- Amended by [[adrs/0053-notes-only-landings-cover-prose|ADR-0053]]: the paths now cover harness prose, ADRs, docs and skills, with their tests.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: the notes-only record is keyed by commit, not by tree.
- Superseded on 2026-10-18 by [[adrs/0062-every-landing-runs-the-full-suite|ADR-0062]]: the notes-only tier is removed and every landing runs the full suite.
