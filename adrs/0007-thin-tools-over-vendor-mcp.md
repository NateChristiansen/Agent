---
id: 7
title: Integrations are a thin tool of ours unless the vendor's MCP earns its context
status: superseded
date: 2026-09-04
supersedes: null
superseded_by: 13
tags:
- adr
links:
- '[[adrs/0013-integrations-outside-core|ADR-0013]]'
---

# ADR-0007: Integrations are a thin tool of ours unless the vendor's MCP earns its context

## Context

Every integration raises the same question: register the vendor's MCP server, or write a small tool of our own against their API? We have answered it three times now, differently each time, by re-arguing it from scratch.

Two things settle it. The first is a cost that is easy to miss: every tool an MCP server exposes loads into every turn. [[adrs/0005-mcp-tools-over-cli|ADR-0005]] set `alwaysLoad: true` for our own server and said to revisit past about 35 tools, and we are close to that. A large hosted vendor server can expose several times that budget on its own, and not every vendor offers a way to scope it.

The second is a failure mode: a server that answers from a cache rather than the live service. Its answer can be stale, nothing at the call site shows it, and its behaviour is someone else's to define.

## Decision

An integration is a thin module of ours: `src/agent_cli/<name>.py`, an `ops.<name>` wrapper, one or two `mcp__agent__*` tools, tests. This holds unless the vendor's MCP server earns its context.

It earns its context when all of these hold:

- the tool surface is scoped, or small enough to load whole
- the credential can stay out of the repo (an ambient session or OAuth, not a token in a file)
- what it reads is unambiguous: a live endpoint, not a cache whose staleness is invisible at the call site
- we would not have written the same thing in under a hundred lines

Where a vendor offers both shapes, we prefer the one that scopes. Where neither shape fits, we write the tool.

## Consequences

- A vendor server that is small or scoped, with an ambient login, stays an MCP server. Integrations whose vendor servers fail the test become thin `bin/agent` subcommands of ours.
- We own more code, and the maintenance that comes with it, in exchange for owning the behaviour. That is the trade.
- The tool budget is now the deciding constraint on integration design, not an afterthought. **The next integration does not get a free tool slot.** It either replaces something, or ADR-0005's ceiling gets re-argued on its own evidence, rather than quietly exceeded.
- A thin tool only wraps what we need, so capability we did not build is capability we do not have. That is often deliberate, but the general shape is a real cost: the vendor's server would have had the feature.
- `doctor` now checks every registered server is enabled and reachable by a permission rule (`_mcp_servers`), so the servers we do keep fail loudly rather than silently not loading.

## Alternatives considered

**A vendor's hosted MCP server with OAuth.** Nothing secret sits in `.mcp.json`. That is the best credential story available, and the reason this was close. Rejected when the server exposes far more tools than the budget with no scoping mechanism. If it ships a tag or scope filter, it is worth revisiting. The thin tool is not load-bearing enough to defend for its own sake.

**A vendor's local MCP server that scopes.** Scoping by flag or allowlist can bring it under budget. Rejected when the credential has to be passed where other processes can read it, or when the vendor does not treat the local server as the maintained path.

**Reusing another repo's tooling.** Rejected: it couples this harness to another repo's setup.
