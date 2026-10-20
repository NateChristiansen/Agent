---
id: 42
title: Simple route and coordination defaults
status: accepted
date: 2026-10-06
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0042: Simple route and coordination defaults

## Context

An audit of one week found the harness mostly developing itself: every attempt ran the full
suite, comment paperwork blocked attempts, peer-manager wakes took most of the managers'
cache-read tokens, and many features were open at once. Most of that cost bought
coordination, not changes.

## Decision

A change takes the simple route by default: one implementer dispatch, scoped attempt checks, one
fresh reviewer with executed probes, `agent land`, and a report to the owner.

- One manager owns a feature. There is no cap on features in progress: parallel work is bounded
  by `dispatch.max_concurrent`, the verify lane and overlapping files, and a feature nobody is
  working on is parked or closed. This replaces the one-product-lane, one-harness-lane limit
  this ADR first stated.
- A plan gets a review from the other client before the owner sees it: a paired peer manager or a
  reviewer on the other client, agreeing to the plan's exact hash. Code gets one fresh reviewer.
- A manager contacts another only for a real conflict: overlapping files or a shared contract.
- An acknowledgement never wakes a peer; a question or decision always does. Informational
  messages may wait (`peers.defer_nonurgent_s`). Pairing two managers on one feature is a setting
  (`pairing.auto_same_feature`), on by default and off for one manager per feature.
- The full suite runs once per landing, as the candidate's pass record (ADR-0041), not per attempt.

## Consequences

- The following dispatches are measured against the earlier baseline
  (the Verification section of harness/safety.md). A configuration that lets a regression escape, acts out of scope,
  or makes most outcomes worse is reverted on its own.
- Explicit pairing, the review dispatch and the owner's plan approval stay as they were.
- Amended by [[adrs/0051-fresh-cross-client-plan-review|ADR-0051]]: one fresh cross-client plan-review replaces pairing and hash agreement.

## Alternatives considered

- Keeping peer review rounds on code between managers: they found little the probe reviewer
  missed and cost most of the tokens. Cross-client plan review is kept: each client finds gaps in
  the other's plans.
- A work-in-progress cap of one product and one harness feature: dropped. Parallel work is what worktrees and dispatch exist for; the audit's costs came from
  wakes, per-attempt full runs and unowned work, each fixed directly.
- Fewer, slower full runs without a selector: the per-attempt cost stays for changed code.
