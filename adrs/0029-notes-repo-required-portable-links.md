---
id: 29
title: A notes repo is required, and every link setup makes works on plain Windows
status: accepted
date: 2026-09-25
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0029: A notes repo is required, and every link setup makes works on plain Windows

## Context

[[adrs/0012-two-repos-harness-and-notes|ADR-0012]] split the harness from the owner's notes and let a clone run without a notes repo, holding the private set as plain gitignored files. Recloning on Windows showed two problems with that. First, plain private files in a harness checkout are what conflict when the owner's harness updates are pulled. Second, every link was an `os.symlink`, which plain Windows refuses without Developer Mode, and the skill links were tracked symlinks that Git for Windows checks out as plain text files.

## Decision

A notes repo is required: `agent setup` with no `--notes` creates `../Agent-notes` with `notes init` and links it, and the plain-files mode is removed. Every link setup makes is portable. It tries a relative symlink first; on Windows, when that is refused, a directory becomes a junction and a file becomes a same-volume hard link (`src/agent_cli/links.py`). The skill links are gitignored and generated the same way (`src/agent_cli/skill_links.py`).

## Consequences

- Doctor fails on a plain private path, on no linked notes repo, and on a `diverged` hard link.
- A hard link cannot cross volumes, so on Windows without symlinks the notes repo must share the harness's drive. Setup refuses otherwise and says why.
- An atomic replace severs a hard link. Setup recovers without deleting: the notes copy wins, and the harness copy is parked under the ignored `.agent/scratch/notes-diverged/`.
- A sibling notes repo survives deleting and recloning the harness, stays out of Obsidian's index, and sits outside every manager worktree. The owner chose it over a nested, gitignored repo for those reasons.
- This amends ADR-0012's no-notes-repo sentences; its two-repo split stands.

## Alternatives considered

- Keep the plain-files mode as an option. Rejected by the owner: it is the state that breaks pulls.
- `core.symlinks=true` in the clone instructions. Rejected: it needs Developer Mode or admin, so no clone command can rely on it.
- Copies instead of hard links for the two files. Rejected: a copy drifts silently, while a severed hard link is detected and recovered.
