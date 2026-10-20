---
id: 36
title: New dispatch ids carry an owner-set machine tag
status: accepted
date: 2026-10-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0036: New dispatch ids carry an owner-set machine tag

## Context

Several machines can share one notes repo (ADR-0034). Local sequence allocation can give two of them the same
id, handoff and derived branch before a sync. Machine tags separate distinct labels, but a
copied local config gives two installations the same label. The handoff guard contains damage
after allocation; it cannot prevent the first collision.

## Decision

New dispatch ids carry a machine tag, `NNN-<tag>-SS`, for example `017-mac-01`.

- The tag is a label the owner sets: `machine:` in `agent.local.yaml` wins, else the ADR-0035
  device name. It is lowercased and must match `[a-z][a-z0-9]{0,7}`; a name that does not fit
  is not used, and `dispatch new` refuses until `machine:` is set. Doctor warns when the tag
  comes only from the device name.
- Seqs count per feature per tag. Handoffs become `<tag>-SS-<short>.md`, with `machine:` and an
  `install:` token in their frontmatter. The token detects duplicate labels; it is never in an id.
- Before the first real allocation, reserve the label for that installation token on the notes
  remote. A pinned-parent commit appends to separate `records/labels.jsonl`; a successful
  fast-forward push grants ownership. Competing pushes refetch and retry. An unanswered push is
  settled by operation id and matching installation ownership. The id ledger and its parser stay
  unchanged.
- Cache successful ownership for later offline allocations. Initial offline use, a foreign claim,
  or a same-label handoff with a foreign or missing token refuses before records, branches or
  worktrees are created. Dry runs inspect without minting tokens or making claims.
- Every derived branch and worktree path carries `-<tag>-SS`, including the first dispatch. A
  `branch_template` without `{suffix}` fails doctor and `dispatch new`. An explicit `branch=`
  and escalation reuse share a branch on purpose, as today.
- Legacy `NNN-SS` ids stay valid, with no migration. Every machine that dispatches on a shared
  notes repo upgrades before its next allocation. Existing ids and handoffs are never renamed.

## Consequences

- A copied label has one winner; the other installation chooses a new label. Reinstallation
  needs a new label. Old code can still allocate without claiming, so all devices must upgrade.
- Doctor reports unverified labels and cache or handoff conflicts. Notes auto-sync excludes both
  claim ledgers and refuses local or staged changes to them.
- A copied installation token and its successful cache evade this identity model. The token
  identifies an installation; it is not hardware attestation. Shared explicit product branches
  still need their own ownership controls.
- A tagged handoff with no local record may belong to another machine or to this machine's
  former install. Doctor and `dispatch show` classify it by tag and install token, report it as
  having no local runtime, and raise no orphan warning. A tag gives no remote tooling.
- An old checkout's parsers do not match tagged names. Its doctor fails on them, which is the
  signal to upgrade.
- Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: doctor's machine-label rows are retired; `dispatch new` still refuses a bad label.

## Alternatives considered

- A random per-dispatch suffix: unique with no config, but hard to type, and it names no owner.
- Per-machine seq ranges: no format change, and they scale if partitioned up front. A missing or
  overlapping range collides silently, and a range can run out.
- Reserving every id by push: makes every launch depend on the remote; a cached label claim
  permits later offline work. Putting labels in the id ledger breaks old numeric-id parsers.
