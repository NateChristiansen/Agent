---
id: 63
title: Landing and pushing need a clean blind code-review round on the head
status: proposed
date: 2026-10-22
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0063: Landing and pushing need a clean blind code-review round on the head

## Context

One review pass misses many real defects, and its findings vary from run to run. On two branches with
ten known blocking defects, one reviewer found at most five. A blind pair, one Codex `gpt-6.1-sol` and
one Claude `sonnet` reviewer at `high` effort on the same contract, found seven, for about $0.80 a branch.
The `dispatch` action `code-review` runs that pair as a round, with a findings ledger the reviewers never
see and one verifier that drops a finding only on a concrete refutation. `push-guard` today accepts any
finished review-row dispatch, and `land` checks no review at all.

## Decision

`land` and `push-guard` refuse a branch head that has no complete `code-review` round with zero verified
blocking findings on exactly that head. The gate covers every branch, including branches already in
flight when it landed. The owner can override either check with a reason, and each override is logged
on the feature. A branch that was in flight lands through that override.

## Consequences

- Every branch pays for one round, about $1, and waits for it before landing.
- A commit after a clean round needs a new round, because rounds are keyed by head sha.
- After `ROUND_CAP` complete rounds on a branch, the action stops launching, and the owner decides with
  the ledger in view.
- Branches in flight when the gate landed need a round or an owner override.
- The "one fresh reviewer" route text changes with this decision; that wording is the owner's.

## Alternatives considered

- One fresh reviewer, as before: cheaper, but it found half the known blocking defects.
- Claude ultrareview: it found six of ten, but it runs only outside dispatch.
- Warn-only, then enforce: rejected in favour of an owner override for branches in flight.
- Exempt in-flight branches by start date: built, then dropped. Two review rounds found ten blocking
  holes in dating a branch's start (rebases, stacked and cherry-picked commits, machines that pull late).
