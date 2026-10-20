---
id: 33
title: Models reach the harness CLI only through the MCP
status: accepted
date: 2026-09-30
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0033: Models reach the harness CLI only through the MCP

## Context

ADR-0005 made the MCP tools the model's front end over the same ops the CLI runs, and kept the
CLI as a fallback. An audit of one week of transcripts found both clients running `bin/agent` in
a shell hundreds of times where a tool existed. Harness text named the CLI first, MCP errors
named CLI flags, a served tool failed on any parameter called `name`, and after an error models
fell back to the shell. Some commands models run had no tool at all.

## Decision

Models never run `bin/agent` in a shell; every action a model takes goes through the MCP.
A `harness` tool, routed by `action`, covers the CLI-only commands models run (scrub, doctor,
eval, notes, journal, scratch, worktree, procs, style). `mcp/coverage.py` maps every CLI command
to a tool or to `PERSON_ONLY` with a reason. A PreToolUse `cli-route` hook on both clients
refuses any model shell call to the CLI and names the tool, or says the owner runs it. There is
no CLI fallback when the server is down: the model asks the owner to reconnect.

## Consequences

- Tests fail when a CLI command is in neither map or a CLI option has no tool parameter.
- MCP results rewrite any CLI reference into the tool and parameter names (`mcp_wording`).
- People, client hooks, git hooks and the dispatch supervisor still run the CLI.
- The guard sees only tool calls a client routes through PreToolUse; it is not a process boundary.
- A server outage stops model record-keeping until the owner reconnects.
- Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: `cli-route` still enforces this; MCP coverage completeness is no longer tested.

## Alternatives considered

- Block only MCP-covered actions and keep CLI-only ones in the shell: two paths stayed, and the
  owner preferred one.
- One MCP tool per CLI command: it would overrun the manager's tool budget.
- Prose only: the audit showed prose already said "MCP first" and did not hold.
