---
id: 39
title: Plan Handoffs are a view over handoff files, not a written list (amends ADR-0006)
status: accepted
date: 2026-10-04
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0039: Plan Handoffs are a view over handoff files, not a written list (amends ADR-0006)

## Context

ADR-0006 made a wikilink under the plan's `## Handoffs` part of every dispatch record. The CLI
adds one line per dispatch there and repaints its status word as the dispatch moves. ADR-0034
syncs the notes repo at each turn's end, and it aborts a rebase that conflicts. ADR-0036 gave each
machine its own dispatch ids and handoff files, so handoff files no longer collide. Plans still
do. When machines dispatch for one feature between syncs, each adds a line at the end of the
same section. Git cannot order two insertions at one spot, so the later machine's rebase stops
on `plan.md`. This was the one remaining unmerged path.

## Decision

No dispatch path writes `plan.md`; the plan's `## Handoffs` section holds one fixed Dataview
view over that feature's own handoff files. The plan template writes the view once. The view
uses the embedding note's folder and reads indexed handoff frontmatter. The handoff files stay
the record. This uses the vault's existing Dataview infrastructure.

ADR-0006's record list changes. "A wikilink under the plan's `## Handoffs`" now reads "the plan's
`## Handoffs` embeds a view over the feature's handoff files". The rest of ADR-0006 stands.

## Consequences

- A notes rebase has nothing to merge in a plan's Handoffs section.
- A plan read as text no longer lists its handoffs. Managers use the `dispatch` tool's `list`
  action or `feature_show`. The dispatch and status skills may need a line saying so.
- Existing generated sections migrate through a preview/apply operation with whole-file hashes.
  It preserves frontmatter, tasks, narrative and handoff files. Unknown section content refuses
  migration. This replaces the earlier decision to wait for active features to close.
- `.claude/rules/features.md` describes the fixed view and the handoff files as records.
- On acceptance, ADR-0006's Consequences gets a one-line pointer here.

## Alternatives considered

- Render writes the section from the handoff files: renders on separate machines still commit different
  text to one spot, so the rebase still conflicts.
- A generated per-feature index file: the same conflict, moved to a new file.
- A notes merge policy for `plan.md` (a union merge driver): it orders nothing and can duplicate
  or reorder lines silently.
