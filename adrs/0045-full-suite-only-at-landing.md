---
id: 45
title: A dispatch never runs the full suite; the landing certificate is the one full run
status: accepted
date: 2026-10-07
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0045: A dispatch never runs the full suite; the landing certificate is the one full run

Amends ADR-0041 (where the full run happens) and ADR-0044 (what a pending verification holds).

## Context

Nearly every dispatch verification ran the full suite: the table's `[[full]]` rules matched
fixtures, core, configuration, hooks, the verification runtime and MCP, and any unmapped path,
empty diff or unreadable table fell back to everything. `agent land` then ran it again for the
candidate.

## Decision

A dispatch launched under selection policy 2 is verified with a scoped selection or refused; the
landing certificate for the exact candidate (ADR-0041) is the one full run.

- Launch stamps `selection_policy`: 2 for a version-2 base table, 0 for a repo with no table. A
  repo that keeps a table refuses a base whose table is missing, unreadable or version 1. An
  escalation keeps its dispatch's policy; a record without the field reads as 1 and keeps the old
  contract through every attempt and resume.
- Version-2 tables turn `[[full]]` into `[[broad]]` rules (same paths and extras, plus the tests
  that cover them) and add a nonempty `[[fallback]]` for unmapped and deleted paths. A broad path
  adds its rule's tests, never its importers; a changed test helper adds the tests importing it.
- An empty diff with HEAD moved runs the fallback after the no-diff exemption and any pass record
  for the tree; a weakening candidate table is ignored for every path.
- No base, a non-v2 base, unreadable git or table, an empty fallback with nothing selected, or a
  selection covering every collected test is refused with its reason: verification `incomplete`,
  no tests run. Policy 2 is never certified at dispatch. ruff, format and pyright run whole.
- A v1 attempt migrating the table without losing a mapping runs full as `selection-migration`.

## Consequences

- A regression only the full suite catches surfaces at landing. Managers batch landings with
  `land --with` so one full run covers several dispatches.
- A dispatch on an old base must be rebased on local main first.
- Selection rows carry their policy; policy-1 digests are unchanged. Broad test lists need upkeep.
- ADR-0044's pending-verification capacity now mostly holds scoped runs.

ADR-0047 exempts notes-only landings (`harness/notes-only.toml`), chained to a full record.

Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: selection policy 1 is deleted; a pre-v2 full row stands, a pre-v2 scoped row is reverified.

Amended 2026-10-18 by [[adrs/0062-every-landing-runs-the-full-suite|ADR-0062]]: the small landing tier (scoped tests plus a smoke set, chained to a full pass) and the notes-only exemption are removed. Every landing gets a full pass record; dispatches still run scoped checks, and `[[broad]]` rules stay for dispatch selection.

## Alternatives considered

- Full runs for core paths at dispatch: most dispatches touch one.
- Falling back to the full suite on uncertainty: that made nearly every run full.
- Trimming an oversized selection: an arbitrary subset proves nothing; refusing names the problem.
