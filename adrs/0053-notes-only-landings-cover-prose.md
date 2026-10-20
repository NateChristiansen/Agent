---
id: 53
title: Notes-only landings cover harness prose
status: superseded
date: 2026-10-12
supersedes: null
superseded_by: 62
tags:
- adr
links: []
---

# ADR-0053: Notes-only landings cover harness prose

## Context

[[adrs/0047-notes-only-landings|ADR-0047]] let a landing that touches only listed paths run
scoped tests instead of the full suite, starting with `harness/lessons/**`. Prose still paid the
full run: a commit changing only one skill reference file took a full pass. The mechanism was right; its path list was too narrow.

## Decision

The notes-only policy covers harness prose: `harness/notes-only.toml` lists
`harness/lessons/**`, `harness/*.md`, `adrs/*.md`, `docs/**/*.md` and `.agents/skills/**/*.md`.
Its scoped tests are the suites that read those files' contents: lessons, the worker contract,
the rubric table, ADR records and links, and skill links.

## Consequences

- `AGENTS.md` (always loaded, byte-budgeted), code, config and `harness/obsidian/**` (scripts
  and templates) still run the full suite. A change to the policy file always does.
  `tests/test_notes_only.py` pins both sides of that line.
- Lines amended in ADR-0047: "`harness/lessons/**` to start" now reads as the five globs above.
  "A lessons-only landing costs the lint and type commands plus `tests/test_lessons.py`" now
  reads: a prose-only landing costs the lint and type commands plus the listed tests.
- Anchoring, the separate notes-only record and the publication rule are unchanged.
- Superseded on 2026-10-18 by [[adrs/0062-every-landing-runs-the-full-suite|ADR-0062]]: prose lands on the full suite like any other change.

## Alternatives considered

- Zero checks for skill prose: skill links and the worker contract are read by tests, so a prose
  edit can still break them.
- A new route for documentation: the existing policy already had the anchor and record rules.
