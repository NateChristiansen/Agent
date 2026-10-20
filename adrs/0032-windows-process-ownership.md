---
id: 32
title: Windows process ownership uses jobs and tagged snapshots
status: accepted
date: 2026-09-28
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0032: Windows process ownership uses jobs and tagged snapshots

## Context

The owner asked for Windows cleanup of the process trees the harness launches. POSIX groups,
`ps -E`, and `os.killpg` do not work there. A timed-out setup command can leave a grandchild
running after its parent exits. Reaping by PID alone can hit a reused PID or a process the
owner marked to keep.

## Decision

Use a Windows Job Object for each bounded command, retained until communication and timeout
cleanup finish. Use a Windows-only psutil dependency to read inherited session and dispatch
tags for the process snapshot. Before reaping a selected process, compare its creation time
with the snapshot and use psutil's PID-reuse-aware kill. Keep `winproc` query-only for liveness
and creation-time reads. A host that refuses job assignment may run a short command; on timeout
it falls back to `taskkill /T`, and reports unconfirmed cleanup through a `TimeoutExpired`
subclass when that fallback fails.

## Consequences

The Windows environment gains one dependency and native process tests. Access-denied or
unknown-start processes cannot be selected for reaping. Popen assigns a process to its job
after creation, so there is a short pre-assignment gap; native CI must exercise actual
parent-child-grandchild cleanup and unrelated-process survival. This decision does not port
the separate POSIX verifier store and watchdog protocol by itself.

## Alternatives considered

`taskkill /T` alone cannot find a child after its parent exits. Toolhelp and WMI expose
parentage but not the inherited environment tags needed after reparenting. Reading another
process's environment directly through undocumented PEB layout would add more Windows API
surface and architecture-specific failure modes than psutil.
