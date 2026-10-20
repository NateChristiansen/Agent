---
id: 34
title: The notes repo syncs itself from hooks
status: accepted
date: 2026-10-02
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0034: The notes repo syncs itself from hooks

## Context

The private notes repo holds features, logs, lessons and the journal. The owner works on more
than one machine. A manager committed and pushed the notes repo by hand, so a forgotten push
before a machine switch left work behind. The push rules (`push-guard`, the `permissions.ask`
entries) match only a model's Bash call; no harness code pushed anything.

## Decision

The harness syncs the notes repo from hooks: it pulls at session start, and commits and pushes
after a turn that changed it, on Claude and Codex alike.

- Pull runs inside `session-start`, with `notes-sync` on UserPromptSubmit as the fallback for a
  client that skips SessionStart on resume. A dirty tree is fetched and not touched.
- Stop commits every staged, unstaged and untracked notes change, then fetches, rebases and
  pushes to the branch's named upstream. A turn that changed nothing makes no commit and no
  network call.
- The work is synchronous, with one deadline per operation and a reserve for cleanup.
- Never a force-push, a stash or `--no-verify`. A rebase conflict is aborted, the local commit
  is kept, and the failure is reported until a later sync converges. A failed abort records a
  halt that only the owner clears.
- The switch (`off`, `pull`, `on`) is per machine and defaults to `off`.
- Files a render regenerates are not tracked in the notes repo.

## Consequences

- A hook's push never meets `push-guard` or the push ask rule. The standing rule that a push
  needs the owner's ask still holds for every model-run push and for this public repo.
- A manager cannot make a partial commit in the notes repo while the switch is `on`.
- No lock stops a manager's write during a rebase. The code detects the new change and skips
  the push; the next trigger publishes.
- Hook timeouts on both clients must exceed the budget plus the cleanup reserve.
- Docs that tell a manager to commit notes by hand, or never to push them, need new wording.
- Amended by [[adrs/0054-notes-publication-cadence|ADR-0054]]: Stop publishes usage at most every 30 minutes, prompt pulls have a 120 s window, `notes_publish` exists.

## Alternatives considered

- A detached push process: the turn ends sooner, but a rebase can collide with the next turn's
  writes, and the process needs reaper and Windows job handling.
- The `obsidian-git` plugin: it runs only while Obsidian is open and points at the vault.
- A shared switch in `agent.yaml`: the sync itself would carry it to every other machine.
- Leaving `agent.yaml` out of the commit: it holds no machine paths, and an exclusion adds a
  permanent dirty state to handle.
