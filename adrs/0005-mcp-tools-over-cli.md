---
id: 5
title: The CLI is exposed to the model as MCP tools; ops.py is the single implementation
status: accepted
date: 2026-09-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0005: The CLI is exposed to the model as MCP tools; ops.py is the single implementation

## Context

at first the model recorded everything through `bin/agent` from the Bash tool. that worked, but every log entry was a shell-quoting exercise. quotes, backticks, `$` and newlines had to go through a heredoc. the permission matcher refused mixed quoting and split on redirects. the owner approved calls by hand until a hook auto-allowed them. the model also drifted back to editing generated files when the shell path felt awkward. dispatched workers need the same recording surface from a headless `claude -p` session with a restricted tool set.

## Decision

the CLI is exposed to the model as MCP tools (`mcp__agent__*`), served by `bin/agent mcp` over stdio, registered in `.mcp.json` with `alwaysLoad: true`. every action lives once, in `src/agent_cli/ops.py`, as a keyword-only function returning `Op(data, text, code)`. cli.py handlers and mcp_server.py tools are thin front ends over it, so `--json` output and a tool's structured content are the same dict. the server never writes to stdout (the wire). session identity comes from a by-pid file the session-start hook writes, then the env, then an explicit `session_id` argument. `--profile worker` registers only `log` (dead-end, surprise, measurement, blocker) and `task_done`, pinned to the dispatch's feature and tasks.

## Consequences

- typed arguments: no quoting, no heredocs, no permission prompts; enums for kind and status.
- one implementation to test; `tests/test_ops.py` proves the CLI json and the tool data match.
- ~25 tools load every turn (about 5k tokens). revisit past ~35 tools.
- `bin/agent` via Bash stays as the fallback for doctor, migrate, obsidian, hook, and when
  the server is down. hook matchers never fire for MCP tools, so the server validates.
- the `mcp` package (2.1.1) is a runtime dependency; hooks import it lazily, never on start.
- amended by [[adrs/0011-harness-state-after-final-audit|ADR-0011]] (2026-09-15): `migrate` is
  gone, the count is 30 against ~35, and the single-implementation rule holds on the CLI side.
- amended by [[adrs/0033-models-reach-the-cli-through-mcp|ADR-0033]] (2026-09-30): the Bash fallback is gone; models never run `bin/agent`.
- amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]] (2026-10-12): `mcp_server.py` is gone; the server is `mcp/server.py`.
- amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]] (2026-10-12): `ops.py` and `cli.py` are now the `ops/` and `cli/` packages.

## Alternatives considered

- keep the shell path and harden the hooks: the quoting problem is in claude's matcher, not
  ours; every fix was a workaround.
- a separate server that shells out to `bin/agent --json`: two processes per call, the same
  quoting problem one layer down, and a second place for output formats to drift.
- deferred tool loading (no `alwaysLoad`): saves tokens but adds a ToolSearch round trip
  before the first `log`, which is exactly when the model reaches for bash instead.
