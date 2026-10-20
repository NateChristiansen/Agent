---
id: 40
title: 'Initiatives: one folder per goal, containing features and child initiatives'
status: accepted
date: 2026-10-05
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0040: Initiatives: one folder per goal, containing features and child initiatives

## Context

The features of one goal ended up scattered across `features/`, and broad features grew to
dozens of tasks. The owner wants small features grouped under one folder per goal, which
holds the goal's plan, research, outcome docs and members. Members whose dependencies are
met run in parallel.

## Decision

A goal lives in an initiative: `initiatives/NN-slug/` holding `plan.md` (goal, owner,
status, acceptance, member manifest), `log.md`, `records/`, `research/`, `docs/`,
`features/NNN-slug/` and child `initiatives/NN-slug/`, recursively.
- Feature numbers stay global within the vault and never change on a move. Initiative ids
  are a separate global sequence. Each initiative has at most one parent; containment has no
  cycles and implies no dependency.
- Statuses: proposed, active, blocked, done, abandoned. `done` means the goal is accepted;
  abandoned members never count toward it. A blocked child does not block a parent with
  other ready work.
- Dependencies are between features by global number and are checked for unknown ids, self
  edges and cycles. An abandoned prerequisite blocks its dependents.
- Id allocation and move or instantiate stages are compare-and-swap claim commits on the
  notes remote: only a fast-forward push from the fetched parent grants a claim; losing
  claims are discarded, never merged.
- Move and instantiate are typed manager tools with pinned digests, staged receipts in the
  initiative's `records/`, and replay and crash recovery. Workers never write these records.
- Parallel members rely on worktrees; overlap is a launch-preview warning, and an agent
  resolves merge conflicts.

## Consequences

Standalone features keep their folder layout, but creating any feature now needs a
reachable notes remote, so offline `feature_new` refuses. `initiatives/` is private. Discovery,
ids, resolution, raw paths, private paths, hooks, views and doctor walk nested folders. A
move rewrites wikilinks and handoff paths and voids review agreements on moved files.

Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: no initiative move or instantiate tool exists.

## Alternatives considered

Flat features with an `initiative:` field: cheaper, but no folder per goal. Blocking overlap
checks: rejected; merge conflicts are an acceptable cost.
