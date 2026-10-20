---
id: 50
title: A fresh setup declares the consumer role
status: accepted
date: 2026-10-09
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0050: A fresh setup declares the consumer role

## Context

ADR-0043 added the consumer role, but nothing wrote it. `harness_role.role` reads a missing
`harness:` block as `source`, so every fresh clone became a maintainer install. Someone
who only runs the published harness could then commit, land and push it.

## Decision

`agent setup` writes `harness: {role: consumer}` when it creates `agent.local.yaml`, without
asking. It never rewrites an existing `agent.local.yaml`. A missing block still reads as
`source`, so installs that predate this keep their role. A maintainer checkout sets
`harness: {role: source}` by hand.

The file is created with an exclusive create (`O_CREAT|O_EXCL`, `harness_role.create_local`).
Linked and dispatch worktrees resolve the main checkout's file, so they never create one.

## Consequences

- A fresh install updates with `git pull --ff-only`, which the main guard allows a consumer.
  It cannot commit, land or push the harness.
- An older second install that should only track upstream needs the role set by hand.
- The `harness:` block in `agent.local.yaml` shows which role an install has. Doctor no
  longer has a row for it.

## Alternatives considered

- Ask during setup. Rejected: a new user cannot judge the question on day one.
- Read a missing block as `consumer`. Rejected: it would silently demote every existing
  maintainer checkout.
