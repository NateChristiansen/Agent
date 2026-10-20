---
id: 31
title: Keep the portable Node hook launcher after native Windows timing
status: accepted
date: 2026-09-28
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0031: Keep the portable Node hook launcher after native Windows timing

## Context

Every client hook starts a new process. The fast `agent hook <name>` entry point already avoids
the Claude SDK and full dispatcher. A Windows CI run measured
30 warm guard calls per route and working directory in native Git Bash and PowerShell. The
configured Node loader's PowerShell median/p95 was 461/481 ms for Claude and 491/506 ms for
Codex at the root; in a nested directory it was 450/692 and 506/708 ms. Git Bash p95 was
317/324 ms at the root and 302/426 ms nested. The venv executable and `uv --no-sync` were
faster, but all configured p95 values were below the shared 2-second handler target. These
are CI shell measurements, not live client events or the owner's two-manager load test.

## Decision

Keep `bin/agent-hook.cjs` as the configured launcher for both clients. It finds the vault
from either client's working directory, sets the root for handlers, prefers the selected
venv executable, and falls back to `uv run --no-sync --project <root>` when the venv is
missing. It also runs the separate caveman JavaScript hooks. Keep the Python fast entry
point so this wrapper does not pay for the SDK or dispatcher on each hook call.

## Consequences

The loader adds a process start: compared with `uv --no-sync`, configured PowerShell median
cost was 40-70 ms at the root and 40-95 ms nested in this run. It keeps one portable command
definition instead of embedding machine-specific venv paths or relying on the current cwd's
project. `doctor` must probe the configured command's real denial and fail if its p95 exceeds
2 seconds or any invocation reaches 5 seconds. The owner-machine two-manager and live event
measurements remain release gates; CI timings alone do not prove them.

Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: the launcher runs no caveman hooks, and doctor has no hook-timing probe.

## Alternatives considered

- Direct venv `agent.exe`: fastest measured route, but a config would need to resolve and
  refresh a machine-specific path after relocation or re-setup.
- `uv run --no-sync agent`: valid in the measured root and nested directory, but a client can
  start from a different project; without an explicit vault root it may select that project.
  Adding cross-shell root discovery would recreate much of the loader.
- System `python3`: the CI interpreter lacked `agent_cli`; it is not a universal launcher.
