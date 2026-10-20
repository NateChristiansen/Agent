---
id: 37
title: Manager worktrees live under one parent that both clients grant (amends ADR-0023)
status: accepted
date: 2026-10-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0037: Manager worktrees live under one parent that both clients grant (amends ADR-0023)

## Context

ADR-0023 put each manager worktree at `../<main>-wt-<name>`. Those paths sit outside both
clients' write grants. On Claude, every edit there asked the owner, though the same edit in the
main checkout did not. On Codex, writes there were refused. Claude's `additionalDirectories`
takes literal paths only, and its ask rules match only under the session cwd. Codex profiles
accept path grants, but the harness generated none for worktrees. One stable parent grant
serves both clients and keeps the owner's protective asks on Claude.

## Decision

Manager worktrees live under one fixed parent, `../Agent-worktrees/<name>`, that setup grants on
both clients in per-machine local config.

This amends one line of ADR-0023's Decision. "A manager worktree is a sibling of the main
checkout, `../<main>-wt-<name>`" now reads "A manager worktree lives under the sibling directory
`../Agent-worktrees/`, at `../Agent-worktrees/<name>`". The rest of ADR-0023 stands.

- Claude: a generated `.claude/settings.local.json` lists the parent in `additionalDirectories`.
  It also repeats each `Edit(...)` ask rule from `.claude/settings.json` as an absolute
  `//<parent>/*/...` rule, so a worktree keeps the owner's asks.
- Codex: the generated profile grants the parent as a writable root.
- Both need a new session after setup. Doctor checks each grant, and warns about a worktree still
  at the old path.

## Consequences

- Ordinary edits in a worktree are meant to need no prompt on either client. That is not yet
  verified live; it needs setup and a new session on each client.
- Codex still has no protective ask anywhere, in the main checkout or a worktree. That gap is
  tracked separately and is not changed here.
- The parent stays a sibling, not inside the vault or `../repos`, for ADR-0023's reason and so
  product-repo guards never treat a harness worktree as product.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: doctor no longer checks the grants or warns about old-path worktrees.

## Alternatives considered

- Under `../repos/`: already granted on Claude, but the product guards would need a carve-out.
- Inside the checkout: needs no grants, but loads the manager instructions twice (ADR-0023).
- Per-worktree entries in local settings: literal paths that change with every new worktree.
