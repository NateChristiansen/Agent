---
id: 3
title: Hooks are Python CLI subcommands, not bash scripts
status: accepted
date: 2026-09-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0003: Hooks are Python CLI subcommands, not bash scripts

## Context

the first spec had five hooks as bash scripts in `.claude/hooks/`, written for macOS's bash 3.2 with jq, each with a colocated `.test.sh`. the CLI they call is python. mid-build, the owner asked for the hooks in python instead. a hook must start fast: a PreToolUse hook that times out fails open. a hook must never wedge a session.

## Decision

hooks are subcommands of the CLI. `bin/agent hook <name>` reads the hook JSON on stdin, dispatches to a function in `src/agent_cli/hooks.py`, and always exits 0. it prints only when it has something to say: session-start context, or a PreToolUse deny. settings.json is the only wiring. hooks call the CLI's own functions directly instead of shelling out. tests live in `tests/test_hooks.py` with the rest of the suite.

## Consequences

- one codebase, one test runner, no bash 3.2 or jq constraints.
- ~90 ms per hook call through the venv, well inside the 5 s timeouts.
- adding a hook is a function plus a NAMES entry plus a settings.json line; `agent doctor`
  checks the wiring matches.
- the `.claude/hooks/` directory does not exist; the first spec's bash sections are superseded.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: hooks are the `hooks/` package with tests split across files, and doctor no longer checks the wiring.

## Alternatives considered

- bash + jq scripts as specced: rejected by the owner; two codebases and a hand-rolled test
  harness for ~200 lines of shell.
- standalone `.claude/hooks/*.py` scripts: slightly faster start, but a second codebase
  calling the CLI through subprocess. rejected in favor of subcommands.
