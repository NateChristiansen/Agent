---
id: 43
title: Consumer installs
status: accepted
date: 2026-10-06
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0043: Consumer installs

## Context

A machine that only uses the harness can drift into a fork that diverges from origin in history
while adding nothing beyond it. Such a machine should be a plain clone that runs the published
harness and contributes nothing back.

## Decision

An install declares `harness: {role: consumer}` in its `agent.local.yaml`, and a consumer's agents
never change the harness: no commit, merge, rebase, land or push in the harness checkout.

- The one main move allowed is a fast-forward to the upstream (`pull --ff-only`, or
  `merge --ff-only origin/<branch>`).
- `agent land` refuses, and the pre-push hook refuses every push from the harness checkout.
- A harness-scope lesson is queued privately, marked `[harness-candidate]`, for the owner to carry
  to the source install. Product repos are unaffected.
- Doctor shows the role and warns when local main is not an ancestor of `origin/main` or tracked
  files are modified. A malformed `harness:` block reads as consumer.

## Consequences

- Harness changes come from the source install only. A consumer takes them with a fast-forward and
  `uv sync`, and needs its own pass records only if it ever becomes a source.
- The conversion runbook also disables the clone's push URL, so a person's push fails too.
- Amended by ADR-0048: lines 29-30 now read as one local lesson queue, filed upstream at review.
- ADR-0050: `agent setup` now writes `role: consumer` when it creates `agent.local.yaml`.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: doctor no longer shows the role, the ancestor check or modified files.

## Alternatives considered

- `agent.exportOnly` alone: it refuses pushes but leaves local commits and lands open.
- A long-lived fork: it diverges in history with nothing to show for it.
