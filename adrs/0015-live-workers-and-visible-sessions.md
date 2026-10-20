---
id: 15
title: Live workers, coordinating sessions and visible manager state
status: accepted
date: 2026-09-16
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0015: Live workers, coordinating sessions and visible manager state

## Context

The Workers list was a 7-day history of question, blocked and failed handoffs. It filled with probes and stale asks, and never showed a done worker waiting on a push. Manager sessions had no names. Two of them deadlocked on vault leases, and the owner had to broker it. Claude Code's tab glyph showed ✳ whether a session was idle or waiting on the owner, and hooks cannot change that glyph.

## Decision

What waits on the owner, and what each session is doing, is explicit state on the harness's own records, written by hooks and the CLI and shown in three places.

- A dispatch waits on the owner until acted on: `ack`, `abandon`, cancel, or a resume that finishes later. One rule, `state.is_live`, drives the session-start block, `dispatch list`, the statusline and the Live Workers view. Eval probes (`kind: probe`) never wait.
- A manager session carries a `mgr-NNN-slug` name and an `activity` (working, waiting on you, idle), both recorded by hooks. Dispatches record the session that launched them.
- Leases coordinate between sessions. A resting holder's lease is taken over, a denied edit records a request, and the holder is nudged and yields at turn end.
- The state shows on the Dashboard's Workers and Managers sections, on the statusline, and on Obsidian Terminal tabs relabelled through the Terminal plugin's own rename path. `dispatch open` opens a worker from its Workers row.
- MCP tool calls run in a fresh process, so the state logic an edit changes is the logic that runs.

## Consequences

- Amended by [[adrs/0016-live-views-over-a-snapshot|ADR-0016]]: the Managers splice is gone; manager and worker state reach the Dashboard through `.agent/live.json`.
- Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: `lease-nudge` is gone; `lease-yield` only stamps the turn end, and a resting lease is free to take.
- ADR-0006's "handoffs are the record" stands. A handoff now also carries `live`, `goal`, `ask` and `open`, refreshed by `status render`.
- New hooks: `activity` (on six events), `session-name`, `lease-nudge`, `lease-yield`. The PostToolUse `.*` pair runs `bin/agent` on every tool call, and that latency is unmeasured.
- Tab labels and `dispatch open` depend on the Obsidian CLI and on undocumented Terminal plugin view state. `nameSource` in `~/.claude/sessions/<pid>.json` is also undocumented. Each fails soft: no label, a printed command, or no rename.
- The Managers section rewrites Dashboard.md on each state change, so the notes repo shows it modified most of the time.

## Alternatives considered

- A time-windowed Workers view: it kept stale asks and hid done work.
- `claude agents --json` for session state: a subprocess on every render, and it cannot say "waiting on worker".
- A state prefix in the session title: a hook can set the title only on a prompt, when the session is always working.
- A deny on a stale MCP server: it would wedge the harness while it is edited.
