---
id: 52
title: No line cap on Python files
status: accepted
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0052: No line cap on Python files

## Context

[[adrs/0026-git-hooks-gate-commits-and-pushes|ADR-0026]] made the pre-commit gate fail a staged
Python file over 500 lines after formatting, matching an existing suite test. Many files sat
just under the cap. Modules were split only to fit it, which grew large re-export `__init__`
files, and the cap forced detours on unrelated fixes.

## Decision

No Python file in this repo has a line cap. The pre-commit gate checks formatting and lint only;
the instruction byte budget stays.

## Consequences

- `LINE_LIMIT` is gone from `gitgate.py`, and `tests/test_file_sizes.py` checks only the
  instruction byte budget.
- Lines amended in ADR-0026, Decision: "It fails on unformatted code, on more than 500 lines after
  formatting, and on lint errors" now reads "It fails on unformatted code and on lint errors".
  Consequences: "A format or size failure now stops the commit" reads "A format failure now stops
  the commit"; "The limits live in `gitgate.py`, and `tests/test_file_sizes.py` imports them" now
  covers only the instruction byte budget.
- Also in ADR-0026, doctor no longer shows push or git-hook state. "`setup`
  installs them while `doctor` fails without them" reads "`setup` installs them". "`doctor` fails
  when a notes repo is linked and `agent.exportOnly` is unset" reads: setup sets the flag only on
  a public-export clone and otherwise tells the owner to set it; doctor does not check it.
- Large modules are judged in review, not by a number. Re-merging split modules is optional.

## Alternatives considered

- Raising the cap to about 1,000 lines: it keeps a number that stands for nothing a reviewer
  checks, and the splitting pressure returns at the new size.
