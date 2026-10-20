---
id: 6
title: Workers are headless claude sessions in per-dispatch worktrees; handoffs are the record
status: accepted
date: 2026-09-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0006: Workers are headless claude sessions in per-dispatch worktrees; handoffs are the record

## Context

the manager must get code changes made in the product repos without becoming an in-session orchestrator: a session that edits other repos from inside its own context and never loads their harnesses. the owner wants to keep talking to the manager while workers run. they want to watch one live when they care. they want their own checkouts left untouched. they want every worker to leave a record they can read in obsidian and in the feature log.

## Decision

a dispatch runs `claude -p` as a headless worker. its cwd sits inside a fresh git worktree of the target repo (`../repos/_worktrees/<short>/<NNN>-<slug>`, branch `agent/<NNN>-<slug>`), so the worker loads that repo's CLAUDE.md, hooks, skills, and MCP servers. our own detached supervisor (`bin/agent dispatch run <id>`) launches it, captures the JSON envelope, and enforces the timeout. it auto-resumes once at 1.5× budget on a budget hit or a first question. it notifies the owner: chat, macOS, or a slack webhook when they are away. the worker gets a standard contract as an appended system prompt, the feature's plan context and tasks, read access to the feature folder, and injected deny rules (never push, never a PR, no destructive git). it also gets the `agent` MCP server in a worker profile (log and task_done only). it returns a schema-enforced JSON object with a nonce the CLI issued. the record is `features/NNN/handoffs/<seq>-<short>.md` (work order, return, thread), a `dispatch` callout on send, a `handoff` callout on return, a wikilink under the plan's `## Handoffs`, and `bases/handoffs.base` on the dashboard. workers branch and commit; the owner pushes.

## Consequences

- the record and the code stay separate: nothing a worker does can write into the vault except through the two worker tools.
- one more state directory (`.agent/dispatches/`, gitignored) and one more generated surface (`handoffs/`, CLI-owned, hook-protected).
- `claude attach` against a supervisor-launched session is unverified; if it fails, the launcher flips to `claude --bg` and the supervisor reads `claude agents`.
- worktrees accumulate until the feature closes; `feature close` runs cleanup and keeps the branches.
- cost is visible per dispatch in the handoff and the log. parallelism is unlimited by design, so the owner watches spend.
- amended by [[adrs/0008-dispatch-policy-after-audit|ADR-0008]] (no auto-resume, no budget
  raise) and [[adrs/0011-harness-state-after-final-audit|ADR-0011]] (2026-09-15: one dispatch
  per repo under the `repo:<short>` lease; no `claude attach` or `--bg`; `say` on the thread).
- extended by [[adrs/0015-live-workers-and-visible-sessions|ADR-0015]] (2026-09-16: a dispatch waits on the owner until acted on; handoffs carry `live`, `goal`, `ask`, `open`).
- amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]] (2026-10-12: notices go to chat and macOS only; no slack, away or notifications-retry).
- amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]] (2026-10-12: handoffs are `<tag>-SS-<short>.md`, listed by a Dataview view in the plan, not a written wikilink).

## Alternatives considered

- in-session subagents (the Agent tool): inherit the manager's harness, not the repo's. rejected; it is the in-session orchestrator's failure mode.
- `claude --bg` as the launcher: attach for free, but the return, timeouts, and retries would depend on undocumented `claude agents`/`claude logs` shapes. kept as the fallback.
- the main clone on a branch instead of a worktree: collides with the owner's open sessions and with a second worker. rejected.
- markdown-only returns parsed by the manager: rejected for a schema-enforced JSON return with a nonce; a handoff written to a file is not a handoff delivered.
