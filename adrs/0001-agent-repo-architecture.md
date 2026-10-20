---
id: 1
title: Agent repo architecture
status: accepted
date: 2026-09-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0001: Agent repo architecture

## Context

the owner wants one place to manage agentic work across every repo they touch. it takes a task in, records it, dispatches workers into the right repo, keeps a live todo list, and improves itself over time. each existing repo has its own harness, some heavy and some light. an orchestration system that runs its workers inside the planning session plans well but codes badly, because those workers never load the destination repo's harness. nothing today records what the owner does across repos.

## Decision

the Agent repo is a manager, not a coder. it never writes product code. a deterministic CLI owns its durable record: `features/NNN-slug/{plan.md,log.md,raw/}`, `adrs/NNNN-slug.md`, a generated `STATUS.md`, `journal/`, and `repos.yaml` describing the clones in `../repos/`, a sibling directory. clones never live inside this repo. claude code loads every CLAUDE.md from cwd to root, so a worker in a nested clone would inherit the manager context. hooks inject STATUS at session start, capture every prompt and reply for free, and deny hand edits to generated files. workers are dispatched as `claude -p` runs with cwd set to the target repo, so they inherit that repo's harness. claude code is the driver; the artifacts stay tool-agnostic (AGENTS.md, `.agents/skills`).

## Consequences

- the model calls `bin/agent` instead of editing generated files, so status never drifts.
- notes cost zero tokens to capture and are distilled later.
- moving the repo requires renaming claude project dirs and any config that points at the old path.
- everything durable lives here, not in `~/.claude`; auto-memory is off.
- amended by [[adrs/0011-harness-state-after-final-audit|ADR-0011]] (2026-09-15): workers run
  through the Agent SDK, and dispatch is built.
- dispatch needs a defined handoff schema before it is useful.

## Alternatives considered

- in-session subagents for dispatch: rejected, they inherit the manager's harness, which is
  the exact weakness of in-session orchestration.
- agent SDK for dispatch: deferred; `claude -p` already gives cwd, json output, budgets.
- claude cowork as the driver: aimed at non-terminal file work, not orchestration.
- a single learnings file for self-improvement: rejected, grows useless; mechanism is chosen
  per improvement (hook, skill, repo harness, playbook, eval).
