---
id: 51
title: One fresh cross-client reviewer per plan version
status: accepted
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0051: One fresh cross-client reviewer per plan version

## Context

[[adrs/0042-simple-route-coordination-defaults|ADR-0042]] had a plan reviewed by a paired peer
manager or a reviewer on the other client agreeing to its hash, with same-feature pairing on by
default. The hash gate often took several rounds a plan. The `plan-gate` hook denied only on
"session not mapped" and never caught a disagreement. A Claude manager in plan mode could not
launch a Codex reviewer, so a feature could stall on it. Codex's
`plan-gate-stop` read chat for approval asks, blocked an unrelated trust question and revived an
old plan.

## Decision

A plan reaches the owner after one fresh, read-only reviewer on the other client agrees to its
exact bytes. The `dispatch` tool's `plan-review` action freezes the plan as
`.agent/plan-reviews/<sha>.md`, embeds it in the work order, and launches the `review` row on the
other client with `writes=[]`. A first summary line `VERDICT: agree` binds to that sha only. A
plan may opt out in its first 15 lines with `review: small — <reason>` or `review: none — <reason>`.
After three reviews of one plan file the owner decides. Manager pairing, `peer agree` and
`plan-gate-stop` are removed, and `pairing.auto_same_feature` no longer exists.

## Consequences

- Claude's `plan-gate` checks the presented plan against an agreeing review of its sha. Trailing
  newlines are normalised; any other edit voids the verdict. At the cap it adds a note and never
  answers `allow`. In plan mode, `dispatch` is allowed only for `plan-review`.
- Codex has no approval tool and no plan hook; it follows the same steps by instruction. So
  `dispatch new` adds a warning, never a refusal, when a `build`, `design` or `max` launch's
  feature has no agreed plan review.
- Lines amended in ADR-0042: the Decision bullet "A plan gets a review from the other client…"
  now reads as this ADR; the sentence "Pairing two managers on one feature is a setting…" is
  void; in Consequences, "Explicit pairing" no longer exists. Its other decisions stand.

## Alternatives considered

- Keeping persistent pairing with a narrower Stop gate: it still needs a second live manager per
  plan, and a lexical gate on chat can block any question.
- Dropping plan review: the owner keeps it; each client finds gaps in the other's plans.
