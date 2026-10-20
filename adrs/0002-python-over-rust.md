---
id: 2
title: Python + uv for the agent CLI
status: accepted
date: 2026-09-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0002: Python + uv for the agent CLI

## Context

the harness needs a CLI that owns the durable files (features, logs, ADRs, status, sessions, capture) and runs as the body of every hook. the owner wanted to learn rust. the CLI mostly does frontmatter, file appends, yaml, json, and wrapping `claude -p`. hooks must start fast: a slow PreToolUse hook fails open.

## Decision

the CLI uses python 3.14 with uv, stdlib argparse, one runtime dependency (pyyaml), and pytest for tests. `bin/agent` is a bash shim: it execs `.venv/bin/agent` when present and falls back to `uv run`. the command surface stays stable, so a rust port can be a later learning project without changing hooks, skills, or docs.

## Consequences

- the owner can read every line the agent writes; iteration is fast; other python developers can hack on it.
- hook latency is ~90 ms through the venv, well under the 5 s timeouts.
- argparse maps 1:1 onto clap if the rust port happens.
- no compile step, but `uv sync` is required once per clone.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: the runtime dependencies are `claude-agent-sdk`, `mcp` and `pyyaml`, plus `psutil` on Windows.

## Alternatives considered

- rust from the start: a good technical fit, but the owner could not review agent-written rust and the design was still moving. rejected for now, not forever.
- node ESM: a workable fit, but the owner preferred python as the fallback.
- bash + jq: fine for five hooks, painful for the CLI; replaced entirely once hooks moved
  into python.
