---
id: 56
title: Retire the plan ledger and event scheduler
status: accepted
date: 2026-10-12
supersedes: 44
superseded_by: null
tags:
- adr
links:
- '[[adrs/0044-ledger-and-scheduler|ADR-0044]]'
---

# ADR-0056: Retire the plan ledger and event scheduler

## Context

[[adrs/0044-ledger-and-scheduler|ADR-0044]] added a durable plan ledger and an event-driven
scheduler to launch ready tasks. It never ran live. `dispatch.scheduler` was set in no repo, and
`.agent/ledger` held only an empty reservations directory and a lock. Modes with no observed
consumer are retired.

## Decision

The plan ledger, sweeps and the event-driven scheduler are retired in audit 5; ADR-0044 is
superseded whole. The path board that keeps concurrent writers apart stays.

## Consequences

- Deleted: `ledger.py`, `ledger_ownership.py`, `sweep.py`, the scheduler, the `ledger` and
  `scheduler` CLI, the `harness` tool's `ledger_*` and `scheduler_tick` actions, and the
  `dispatch.scheduler` key.
- Kept: the dispatch tool's `writes` and the path board in `dispatch/write_scope.py`, stored in
  `.agent/dispatch-board/`. A launch or resume whose paths overlap active work is refused under
  the board lock. `dispatch.max_pending_verify` still bounds waiting verifications.
- A manager launches every ready, disjoint task itself, as AGENTS.md says.

## Alternatives considered

- Keeping the scheduler off by default: unused code with its own tests and docs still costs
  upkeep, and nothing had opted in.
