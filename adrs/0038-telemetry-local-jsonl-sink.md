---
id: 38
title: Telemetry goes to a locked local JSONL sink in the main checkout
status: proposed
date: 2026-10-04
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0038: Telemetry goes to a locked local JSONL sink in the main checkout

## Context

The harness needs timing data: suite and worktree-setup times, dispatch cost, and review and
rework history. The writers are many concurrent, short-lived processes across several worktrees,
on macOS and Windows: hooks under a 5 s timeout, pytest, supervisors and managers. A sink must
never break a writer, must bound how long it waits on others, must show its own losses, and must
leave a path to a central store. Events name sessions and costs, so they stay out of tracked files.

## Decision

Telemetry events are JSON lines appended to a daily file,
`<main>/.agent/telemetry/events/YYYY-MM-DD.jsonl`, by a stdlib-only recorder.

- `<main>` is the main checkout of the git common dir, so every worktree, dispatch tree and
  temporary verification checkout writes to one gitignored sink.
- Each event is one `os.write` under an exclusive lock with a 50 ms deadline: `flock` on POSIX,
  the existing `msvcrt` byte lock on Windows. A short write or a timeout is a loss, never an
  exception in the caller.
- Losses go to an independent loss file in the temp directory. Per-process writer ids with
  sequence numbers expose gaps. Health reports observed losses only, and says that silent ones
  are not counted.
- Code that knows its root (supervisor, manager actions) records explicitly. Environment-driven
  recorders (the pytest plugin, hooks) record nothing inside a dispatch unless the supervisor
  passes a sink, so workers never write to the manager's state.
- Prompt bodies, outputs and file contents are never recorded. The schema is versioned.

## Consequences

The recorder adds no dependency. On macOS it measured 0.031 ms p50 per event, imports excluded,
with no loss under eight concurrent writers. Windows is not measured; a Windows CI concurrency
test is required before this ADR is accepted. Only the lock wait is bounded: a slow filesystem
can still delay the open or write. A power loss can drop the newest events (no fsync). Each line
maps to an OTLP log record, so a shipper can forward them later. Readers skip bad lines.

## Alternatives considered

OpenTelemetry now: new dependencies, no stable Python file exporter, about 28 ms import per
process, loss at exit. SQLite WAL: measured 0.58 ms p50, 1.66 ms p99 per insert, plus busy
waits. `prometheus_client` textfile: metrics only. `logging`/`structlog`: no multi-process safety.

## Amendments

**2026-10-05, repair.** Corrections from operating the sink:

- *Test isolation.* Fixture harnesses resolved the live checkout through `main_checkout`
  whenever the pytest temp root sat inside it, and explicit recording (which ignores the
  environment by design) wrote there, so most of the live sink's events came from pytest
  writers. The suite now fences
  git discovery at its temp root and the recorder refuses any sink outside
  `AGENT_TEST_SINK_FENCE` while that variable is set.
- *Exclusions, not rewrites.* A machine-local `<sink>/exclusions.json` lists event ids that
  operational counts skip, with the reason; raw lines are never edited. An install without one
  counts everything.
- *Windows lock budget.* The Windows lock wait is 250 ms: its 10 ms retry loop runs at the
  ~15.6 ms timer and got about three tries in 50 ms, which lost one event in 500 under eight
  concurrent writers in CI.

