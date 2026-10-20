---
id: 44
title: An operational ledger and an event-driven scheduler launch approved plans
status: superseded
date: 2026-10-06
supersedes: null
superseded_by: 56
tags:
- adr
links:
- '[[adrs/0056-retire-ledger-and-scheduler|ADR-0056]]'
---

# ADR-0044: An operational ledger and an event-driven scheduler launch approved plans

## Context

Ready, disjoint work waited for a manager's turn while workers sat idle. Features were planned
as chains, and one verify lane held work to one worker at a time. Launch decisions lived
in each manager's head, so a crash, a restart or two managers could launch a task twice or not
at all, and nothing could launch the next ready task when a slot freed.

## Decision

An approved plan is registered as tasks in a durable ledger, and a scheduler run on lifecycle
events launches whatever is ready.

- Ledger: `<main>/.agent/ledger/<feature>/` holds `plan.json` (approved version, state: active,
  paused, cancelled, superseded), `<task>.json` (prerequisites, writable paths, attempt limit,
  launch spec, a contract digest of those, state and attempts) and `<task>.claim` (the launch
  identity, published complete by hard link). One flock per feature serializes register, state
  changes, claims, bindings, completions and reconciliation. A launch completes only the task
  contract it was claimed under; recovery reconciles claims against dispatch records before any
  retry. Prerequisites may cross features and count only while their landed
  commit is on main. Machine-local, never committed, outside workers' writable roots.
- Scheduler (`dispatch/scheduler.py`): per repo `dispatch.scheduler: off | shadow | live`, off by
  default. A pass, serialized by `scheduler.lock`, syncs each plan from dispatch records, then
  starts each ready task whose writable paths overlap no claimed task or active manual dispatch,
  when a repo slot is free and the queue of finished work awaiting verification
  (`dispatch.max_pending_verify`) is not full. Shadow logs the same decisions without launching.
  Every decision goes to `.agent/ledger/scheduler.jsonl`. A dispatch ending, a slot release and
  a landing each start a detached pass.
- Manual dispatches declare `writes`; a launch overlapping running work is refused under the same
  lock, and one that declares none holds back scheduled work in its repo.
- Endings: a failure retries under the attempt limit; a block waits for the owner, who retries by
  registering the plan again; a cancel is final.
- Sweeps (`ledger sweep`) expand a template over disjoint shards into tasks, with a reduce step
  whose `when` (settled or landed) defines what failed and cancelled shards mean.
- CLI and MCP: `ledger register|sweep|show|set`, `scheduler tick`, and the `harness` tool's
  `ledger_register`, `ledger_sweep`, `ledger_show`, `ledger_set` and `scheduler_tick` actions;
  `dispatch new --writes` and the dispatch tool's `writes`.

## Consequences

- Fan-out no longer depends on a manager remembering to launch the next wave, and a duplicate
  event, a second scheduler or a crash between claim and launch cannot launch a task twice.
- A repo must opt in, and should run a workload in shadow before live.
- Manual dispatches that skip `writes` slow scheduled work in their repo; that is the price of not
  guessing which paths a hand-written work order changes.
- Speculative execution, best-of-N and culprit isolation for failed batches stay out until each
  has its own contract (parent versions, invalidation, a redo budget).
- ADR-0045: pending verifications now mostly hold scoped runs; the full suite runs at landing.

## Alternatives considered

- A phase graph inside the manager prompt (the earlier design): the decisions were not
  durable and no event could act on them.
- Claims in the dispatch records alone: a crash between claiming and writing the record could not
  be told from a launch that never happened.
- Treating a blocked dispatch as a failure to retry: it relaunched work waiting on the owner.
