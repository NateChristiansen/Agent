---
id: 64
title: Claude manager cost comes from the priced transcript; no status line
status: accepted
date: 2026-10-24
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0064: Claude manager cost comes from the priced transcript; no status line

## Context

The Claude Code status line ran `agent status line` every 15 s and on each message. It showed the manager name, spend, peers, waiting obligations and leases. Codex has no command status line, and the owner stopped using it. The status line was also the only writer of a Claude manager's cost: it recorded Claude Code's `cost.total_cost_usd` through `sessions.record_cost`. ADR-0022 kept that provider total authoritative and priced only Codex from usage. Measured on 2026-10-24: Codex manager cost read only the root rollout and missed its subagent and guardian rollouts, which hold 5.5% of priced spend plus 246M unpriced `codex-auto-review` tokens.

## Decision

Both clients' manager sessions are priced on `Stop` from their own session files and recorded as `price-estimate`. The status line, the `agent status line` command and the code that fed only it are removed.

- Claude: `prices.price_claude_session` prices the transcript plus its subagent transcripts, one request id counted once.
- Codex: the price sums every rollout that shares the root `session_id` (subagent and guardian children), one response id counted once. `codex-auto-review` tokens are priced at the rates of the guardian's parent thread's model. A child with no usage rows adds $0.00.
- Pricing runs only for a manager `Stop`; subagents, dispatch workers and other events never price. An unpriceable session is marked unknown, never $0.00, including one the status line had priced before.
- Live state stays on the Dashboard and the Obsidian Terminal tab labels (ADR-0015).

## Consequences

- One method and one price table (`src/agent_cli/prices.py`, which now carries 1h cache-write rates) for both clients.
- The Claude estimate runs low: the transcript omits some billed requests. Across seven local sessions it read 3.9% to 22.8% under Claude Code's own figure. The owner accepted this until a follow-up feature records Claude's own per-request `cost_usd` from OpenTelemetry, with the transcript as its fallback.
- Every figure is an API-equivalent estimate, not the subscription bill. The guardian rate is a stand-in until OpenAI publishes one.
- Cost moves only at `Stop`. A turn killed mid-way is counted at the next `Stop`, if one comes. A forked session prices its copied history again. Whether `/clear` or `--resume` copies history into a new session id is unmeasured.
- When a lower estimate replaces a higher status-line total, the earlier days keep the old amounts. Bumping `PRICED_AT` for Claude also restamps Codex estimates' `cost_priced_at`.

## Alternatives considered

- Keep an empty status line to record Claude's exact total: a Claude-only settings entry and handler for one number.
- Claude Code's `cost-state` transcript row: undocumented, and a live session has none mid-session.
- OpenTelemetry for both clients now: Claude's matched its own total exactly in a probe, but it needs a supervised receiver. Codex OTel adds only a 2-token warm-up over the rollout. Deferred to the follow-up.
- Drop manager cost: the owner wants the journal and Dashboard cost kept.
