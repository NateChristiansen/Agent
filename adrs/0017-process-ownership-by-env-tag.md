---
id: 17
title: Processes are owned by an inherited environment tag, and reaped along the ownership tree
status: accepted
date: 2026-09-18
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0017: Processes are owned by an inherited environment tag, and reaped along the ownership tree

## Context

A `playwright install` from a dispatch's setup step was found still running days later, holding
the machine-wide browser-cache lock. Setup ran through
`subprocess.run(shell=True, timeout=...)`, which on timeout kills only the `/bin/sh`; the download
under it was reparented to launchd. Probes showed Claude Code does reap its own background tasks
when a session ends, but not a process that has escaped — a control run's escaped child survived.

## Decision

A process belongs to whoever's environment tag it inherited: `CLAUDE_CODE_SESSION_ID` for a
session's, `AGENT_DISPATCH_ID` for anything in a dispatch's tree, setup included. The tag survives
leaving the process group and being reparented to launchd; a process group survives neither.
Reaping follows the ownership tree: a supervisor reaps its dispatch's tagged processes on every
way it ends, and a manager's `SessionEnd` hook reaps escaped processes carrying its session id.
Harness subprocesses that can spawn run through `procs.run_bounded`: own process group, no stdin,
and the whole group killed on timeout.

## Consequences

- Workers outlive their manager, as before: a supervisor is started with no `CLAUDE*` variable,
  so it never carries its manager's session id. Supervisors are never selected by a reaper.
- The owner's keep-alives (`agent procs keep <pid>`) survive every reap. A keep records the
  process's start time, so a reused pid is never protected by mistake.
- `doctor` reports orphans: tagged, escaped, owner gone, not kept. `agent procs reap` clears them.
- A process that clears its own environment is invisible to all of this.

## Alternatives considered

- **Process groups alone.** Already used for `cancel`, and adopted for `run_bounded`, but a child
  that calls `setsid` leaves the group and cannot be found again.
- **Working directory.** Several manager sessions share the vault as cwd, so it cannot tell whose
  a process is, and killing another live session's process breaks the ownership rule.
- **A manager hook only.** Claude Code already reaps attached tasks; the owner kept the hook as a
  second line for escaped ones, but it could never have caught a supervisor-launched setup step.
