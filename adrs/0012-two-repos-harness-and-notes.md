---
id: 12
title: 'Two repos: the harness is public, the notes are private, setup links them'
status: accepted
date: 2026-09-15
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0012: Two repos: the harness is public, the notes are private, setup links them

## Context

The harness and the owner's notes grew up in one repo. The owner wants the harness public,
improved from any machine, and pulled into their own vault without carrying the notes along.
A one-way template build gives
copies that drift. A fork with publish tooling needs every commit to stay on one side of a
line and conflicts on shared files over time.

## Decision

The harness and the notes are two git repos, and `agent setup --notes <path>` links the notes
into the harness clone with symlinks. The harness repo is the Claude Code project root and the
Obsidian vault root: code, skills, hooks, rules, docs, `.obsidian`, ADRs, the generic
playbook and rubric. The notes repo holds `features/`, `journal/`, `notes/`, `harness/owner/`,
`harness/voice.md`, the eval work orders, `repos.yaml`, `.mcp.json` and the generated
STATUS, Dashboard, Index and bases; the harness gitignores those paths and ships examples.
Without `--notes`, setup creates the same set from the examples as plain gitignored files.
Public history starts at one squash commit; the notes repo keeps the old history.

Lessons split into three tiers, and `/lesson` picks the tier from the mistake's evidence:
`harness/playbook.md` for what holds in any clone, `harness/owner/playbook.md` for what only
holds for this owner, the product repo's own harness for one repo's conventions. Rules and
rubric calibration split the same way.

## Consequences

- Amended by [[adrs/0016-live-views-over-a-snapshot|ADR-0016]]: STATUS.md, Dashboard.md and Index.md left the private set and are plain generated files.
- Amended by [[adrs/0029-notes-repo-required-portable-links|ADR-0029]]: a notes repo is required (setup creates `../Agent-notes`), and links are junctions or hard links on plain Windows.
- Amended by [[adrs/0030-agent-yaml-config|ADR-0030]]: `repos.yaml` in the private set is now `agent.yaml`, with a per-machine `agent.local.yaml` that is never linked.
- Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: eval work orders left the private set, and calibration rows are no longer written automatically.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: there is no tracked `harness/playbook.md`; lessons go to one local queue.
- A harness improvement is one commit on the harness repo; `git pull` brings it anywhere.
- `core.ROOT_MARKER` is `bin/agent`, and atomic writes resolve symlinks before replacing.
- Doctor checks every link; a missing notes repo is a warn, not a fail.
- Symlinks need developer mode on Windows, which was already a caveat.
- `adrs/` in the harness repo holds the harness's ADRs.

## Alternatives considered

- A fork with publish tooling: one repo, a hook forbidding mixed commits, cherry-picks to
  public. Rejected: discipline instead of structure, and shared files conflict over time.
- A notes submodule at `vault/` with Obsidian rooted there: rejected because the Obsidian
  terminal and the Claude project root would no longer be the same directory.
