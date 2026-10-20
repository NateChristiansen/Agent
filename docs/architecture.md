# Architecture

Agent turns an owner's request into a tracked feature, an isolated worker artifact and a
verified landing. Python owns decisions and state; client tools and Obsidian present them.
Paths below are relative to `src/agent_cli/` unless stated otherwise.

## Ownership map

| Package or module | Owns |
|---|---|
| `entry.py`, `__main__.py`, `cli/` | CLI startup, arguments and output. |
| `mcp/` | Typed tools and fresh operation processes; the server caches schemas. |
| `ops/` | Application actions shared by CLI and MCP, returning data and readable text. |
| `config_file.py`, `repos.py`, `repos_validate.py` | Shared/local configuration, repository registry and validation. |
| `features.py`, `task_check.py`, `documents.py`, `log.py` | Feature plans, task transitions, pickup evidence and history. |
| `dispatch/` | Work orders, allocation, permissions, provider transports, supervision and verification. |
| `land.py`, `land_journal.py`, `land_recover.py`, `verify_record.py` | Candidate certification and recoverable movement of local main. |
| `hooks/` | Client event handling, policy guards, activity and notice consumption. |
| `records/`, `core.py` | Record validation, atomic filesystem helpers and runtime path registry. |
| `sessions.py`, `session_identity.py`, `leases.py`, `procs.py` | Session identity, liveness, resource ownership and cleanup. |
| `peers.py`, `peer_net/` | Local mailboxes and restricted cross-device SSH delivery. |
| `notes.py`, `notes_writer.py`, `notes_sync.py` | Private-vault links, renderer ownership and notes synchronization. |
| `status.py`, `vault/`, `obsidian_tabs.py`, `tab_drain.py` | Generated views, live snapshots and fallback tab painting. |
| `journal/`, `usage/`, `telemetry/` | Daily-note blocks, usage collection and append-only measurements. |
| `setup.py`, `doctor/`, `gitgate.py`, `push_records.py`, `export.py` | Installation, health checks and public commit/push gates. |

Facades preserve public imports. Patch a name where its implementation looks it up; a re-export
is a separate binding. Lazy imports keep provider SDK initialization off frequent hook paths.

## One work order, from request to land

1. Intake records a feature through `ops/features.py` and `features.py`. Session mapping adopts
   its initial captures; `task_check.py` flags stale open tasks for a manager decision.
2. `mcp/specs_dispatch.py` or `cli/dispatch.py` calls `ops/dispatch.py`, then
   `dispatch/lifecycle.py`. Preflight scores commands; `dispatch/orders.py` publishes stable
   preview bytes and their link before approval. Distinct preview ids allow a wave of orders.
3. Allocation reserves identity, write scope and holdings. `dispatch/worktrees.py` prepares the
   worker checkout; `dispatch/supervisor.py` starts a detached supervisor. `dispatch/driver.py`
   and `dispatch/codex_transport.py` run the provider connection, steers and bounded attempts.
4. `dispatch/evaluate.py` judges the return and git artifact. `dispatch/verification_gate.py`
   coordinates scoped checks, process cleanup and holdings. `dispatch/terminal.py` commits the
   terminal record; handoff rendering and notification run outside that record lock.
5. Notices reach `hooks/notice_inbox.py`. `dispatch/verdicts.py` tracks reading separately from
   transport acceptance, branch resolution and cleanup. The manager reads and reviews the artifact.
6. `land.py` builds and certifies the exact candidate, then journals file, index and ref changes.
   `land_recover.py` handles interruption. Landed-tip trailers preserve member and origin evidence;
   the manager accepts tasks only after checking the requested outcome.

## Where state lives

| Store | Contents and writer |
|---|---|
| `.agent/dispatches/`, `dispatch-board/` | Runtime worker records, streams, verification evidence and write reservations; dispatch modules write them. |
| `.agent/sessions/`, `leases/`, `tabs/` | Identity, activity, resource holdings and painter queues; session helpers, hooks, leases and painters write them. |
| `.agent/work-order-previews/`, `plan-reviews/` | Approval bindings and reviewed plan snapshots; order and plan-review operations write them. |
| `.agent/dispatch-notices/`, `peers/`, `peer-net/` | Durable notices, inboxes and cross-device delivery/deduplication; dispatch, peer and receiver operations write them. |
| `.agent/raw/`, `telemetry/` | Captured conversations and events; capture hooks and telemetry writers append them. |
| `.agent/usage/`, `journal/` | Usage jobs and pending journal milestones; usage collectors and journal operations write them. |
| `.agent/land/`, `verify/` | Landing journals and certificate evidence; landing and verification modules write them. |
| `.agent/live.json`, `status.json` | Derived, disposable snapshots; `vault/live.py` and `status.py` publish them. |
| Linked notes vault | Plans, orders, logs, handoffs, decisions, ADRs and journals; typed operations and authorized renderers write them. |
| Git | Source, branches, committed notes, public ADRs and landed-tip evidence; workers own artifact branches and landing moves harness main. |

`core.py` registers runtime subtrees. `.agent/` is local operational truth, not a second committed
plan. Handoffs derive from dispatch records, never the reverse. Only the designated checkout
renders shared handoff fields (`notes_writer.py`); Obsidian's plugin displays Python snapshots.

## Five reading hotspots

These are measurements from one point in time, not size limits or instructions to remove checks.
CC counts branching complexity; MI combines size, complexity and comment density.

| Location | Baseline | Why it is large |
|---|---|---|
| `dispatch/codex_transport.py`, `_run_codex` | CC 74 | One app-server attempt handles streaming, tool events, permissions, deadlines and shutdown. |
| `repos_validate.py`, `validate_dispatch` | CC 70 | Many independent configuration keys need type, range and cross-field validation. |
| `dispatch/evaluate.py`, `_decide` | CC 67 | A decision table combines return status, transport outcome, denials and git facts. |
| `dispatch/lifecycle.py`, `new` | CC 65 | Launch binds preview approval, preflight, allocation, worktree preparation and rollback. |
| `tab_drain.py` | MI 0.00 | The fallback coordinates locks, wakes, plugin health, snapshot authority, retries and expiry recovery. |

Read the relevant contracts before changing a seam. Process containment, durable notices and
unknown-state labels supply guarantees that complexity scores cannot judge.

## Running checks

From a checkout with its development venv installed:

```sh
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright
.venv/bin/python -m pytest tests/test_prose_rules.py tests/test_docs_refs.py -q -p no:cacheprovider
```

Pyright checks `src` in strict mode. The prose test rejects history ids in source comments and
docstrings; the docs test validates paths, symbols, commands and tool references. Workers also
run focused tests for their change; the supervisor selects checks using `tests/selection.toml`.
Landing owns full-suite verification of the candidate (see [ADR-0045](../adrs/0045-full-suite-only-at-landing.md)).
