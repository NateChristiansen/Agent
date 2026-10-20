---
id: 18
title: Supervisor verification evidence gates every route to done
status: accepted
date: 2026-09-18
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0018: Supervisor verification evidence gates every route to done

## Context

Worker-reported command strings currently satisfy required-check regexes.
The owner chose supervisor reruns; collect and recovery are also a bypass risk.
A model worker can finish while a separate verifier is still running.

## Decision

Require supervisor-owned verification evidence on every route to accepting work as done.
Configure runnable `dispatch.verify` command/timeout mappings and an explicit total
`verify_timeout_seconds`, separate from `required_checks` report regexes.
Bind versioned evidence to dispatch, attempt, verifier run, final commit, and config digest.
Record ordered outcomes and pre/post HEAD, tracked-file and index observations.
Require a fresh clean-tree observation at final acceptance. Missing, partial, stale, or
legacy evidence cannot prove success. Record authorized no-check exemptions separately.
Verification has its own process lifecycle and completion state. Cancellation must stop
its process group and preserve the lease through cleanup, regardless of worker completion.

## Consequences

The schema and pure validator can be implemented before the runner and completion gate.
They do not execute checks, authenticate stored files, or enforce the lifecycle themselves.
Runner integration must persist protected atomic snapshots and gate run, collect, resume,
and recovery. Each new verification execution gets a new run ID; terminal evidence stays fixed.
Existing registries remain readable without verification config but are unconfigured.
Work dispatches will require runnable verification when integration lands; rollout is separate.
Amended by ADR-0041: reusing a full pass record for the exact final head is also a route to done.

## Alternatives considered

Correcting documentation alone would leave self-reported success as the gate.
Checking only in the normal supervisor loop would leave recovery able to bypass it.
Reusing worker completion would let cancellation finish before the verifier stops.
