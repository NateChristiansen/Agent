---
id: 10
title: A worker's browser is Microsoft's Playwright MCP seeded with a login the owner saved
status: superseded
date: 2026-09-14
supersedes: null
superseded_by: 13
tags:
- adr
links:
- '[[adrs/0007-thin-tools-over-vendor-mcp|ADR-0007]]'
- '[[adrs/0013-integrations-outside-core|ADR-0013]]'
---

# ADR-0010: A worker's browser is Microsoft's Playwright MCP seeded with a login the owner saved

## Context

Some worker tasks need a browser, and this harness had none.

Two constraints shape the answer. [[adrs/0007-thin-tools-over-vendor-mcp|ADR-0007]] says an integration is a thin tool of ours unless the vendor's MCP earns its context. A browser cannot be a hundred lines of ours. And the manager routes; it does not do the work. Browser work is a worker's job. The browser has to reach a headless worker that is logged in as the owner, without the credential ever entering a repo or a work order.

## Decision

- **Microsoft's Playwright MCP server (`@playwright/mcp`) is the browser, for workers only.**
  A dispatch launched with `--browser <env>` gets it as a second MCP server, headless and
  isolated, with `mcp__playwright` allowed. The manager's own `.mcp.json` does not get it;
  the manager never drives pages.
- **The login is the owner's, saved once per environment.** `bin/agent browser login <env>` runs Playwright's own `open` on their installed Chrome (headed), against an environment from the new repos.yaml `browser:` block. When they close the window, the context's storage state lands in `.agent/browser/<env>.json`. That file is gitignored with `.agent/` and is treated as a secret: never logged, never copied into a worktree, only passed to the Playwright server as `--storage-state`. Isolated contexts mean two workers can share one login.
- **A browser dispatch refuses to start without a saved session**, before a worktree or a record exists. `bin/agent browser status` reports age and the earliest cookie expiry; login again is the fix. Screenshots go to `.agent/dispatches/<id>.browser/`, so a return can cite them.

Against ADR-0007's test: the surface is one server, loaded only for a browser dispatch. The credential stays out of the repo. What it reads is a live page. No hundred-line tool could replace it.

## Consequences

- Browser work is a dispatch like any other, with a work order, a return and a record. The
  worker sees exactly what the owner would see, as the owner.
- Sessions expire. A worker that lands on a login page returns `blocked` rather than trying to sign in; the owner re-runs `browser login`. The state file is only ever on the owner's machine.
- Superseded by [[adrs/0013-integrations-outside-core|ADR-0013]] (2026-09-16): the browser is a
  `worker_mcp:` profile the owner configures, and `agent browser` and `--browser` left core.

## Alternatives considered

- **Claude in Chrome from the owner's own session.** Still an option for the owner, but it is
  not a dispatch: no record, no budget, no return, and it costs the owner the hour.
- **A persistent Chrome profile (`--user-data-dir`) instead of storage state.** One profile
  cannot be opened by two browsers at once, so two workers, or a worker and the owner, would
  collide. Storage state seeds a fresh isolated context each launch.
- **The manager holding the browser too.** Tempting for spot checks, against the routing rule
  and the tool budget. If a manager-side browser is ever wanted, it is a separate decision.
