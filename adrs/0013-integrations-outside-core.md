---
id: 13
title: Integrations are vendor MCP servers configured outside core
status: accepted
date: 2026-09-16
supersedes: [7, 10]
superseded_by: null
tags:
- adr
links:
- '[[adrs/0007-thin-tools-over-vendor-mcp|ADR-0007]]'
- '[[adrs/0010-browser-via-playwright-mcp|ADR-0010]]'
---

# ADR-0013: Integrations are vendor MCP servers configured outside core

## Context

[[adrs/0007-thin-tools-over-vendor-mcp|ADR-0007]] made each outside service a thin tool of
ours, such as a ticket tracker, unless the vendor's own MCP server earned its context.
[[adrs/0010-browser-via-playwright-mcp|ADR-0010]] then wired one vendor server into
`dispatch --browser`.

Both put one owner's own services into the harness itself: modules, manager tool slots,
setup rows, doctor checks and permission rules that no other owner needs. The harness is now
a public core plus a private overlay repo, and every one of those counts as owner-specific.
ADR-0007's reasons for writing our own no longer hold either: vendors ship hosted MCP servers
with OAuth, so no key on disk, filterable tool sets, and live data rather than a snapshot.

## Decision

Core ships no integration with an outside service. The manager's servers are the owner's
`.mcp.json`, enabled per machine. A worker's extra servers are named `worker_mcp:` profiles in
`repos.yaml` that `dispatch new --mcp <profile>` hands to one dispatch.

- Removed from core: every owner-specific tool, its setup rows, and the doctor check for its
  credential.
- Core keeps generic seams: `worker_mcp:` profiles with a `requires_file` launch check and an
  exec-from-output-dir launcher, `tickets:` in place of any one tracker's field, and doctor's
  generic check that every registered server is enabled and reachable by a rule.
- The overlay repo carries the vendor servers, their permission rules and scoping, the skills
  that drive them, and the rules that came from those services.

## Consequences

- The manager profile serves 25 tools, down from 29. The ~35 budget now covers the owner's
  vendor servers too, so each is scoped with its own filter flags or headers.
- Until the overlay repo exists, the owner's vault has none of those tools, and a browser dispatch
  needs a `worker_mcp:` profile.
- A new service is configuration, not code: a `.mcp.json` entry or a profile, plus rules. It
  gets no harness test, so a wrong rule shows up as a doctor warning or a prompt.

## Alternatives considered

- **Keep the thin tools in core, off unless configured.** Leaves one owner's code, tests and
  tool slots in every clone.
- **Move the thin tools into the overlay repo.** Core has no plugin seam, so the fork would
  carry edits to the CLI, the MCP server and dispatch, and conflict on every upstream merge.
- **A Claude Code plugin in a private marketplace.** Viable later for distributing the overlay.
  It adds a second packaging format before there is a second consumer.
