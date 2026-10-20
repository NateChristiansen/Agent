---
id: 22
title: Price Codex manager sessions in dollars from rollout usage
status: accepted
date: 2026-09-23
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0022: Price Codex manager sessions in dollars from rollout usage

## Context

The owner chose one dollar measure for manager sessions across Claude and Codex. Claude Code
provides `cost.total_cost_usd` in its status line payload. Codex supplies token counts and a
model in its rollout, but its own in-client dollar estimate is Enterprise-only.
`journal/tokens.py` chose “Tokens, not dollars, on purpose” because a table goes stale and
subscription usage is not cash spent. `sessions.py` stored only Claude's provider total, and
`dispatch/usage.py` kept running worker usage in tokens for the same reason. This ADR
supersedes the no-price-table reasoning for Codex manager sessions. The old reasoning still
defines the limits of an estimate.

## Decision

Price Codex manager sessions in API-equivalent USD from rollout response usage. Keep Claude's
provider-reported dollar total authoritative. `src/agent_cli/prices.py` is the only price table;
each row names its model and Standard input, output, cache-read, cache-write and long-context
rates. The table carries the date checked against OpenAI's published pricing. Price each response under its
turn's model because a cumulative token total hides model switches and long-context tiers.
Unknown models, missing usage, and incomplete coverage produce null cost, never $0.00.

## Consequences

Manager status, the Dashboard and journal carry source and unknown state. A known zero is
different from no price. The estimate is comparable across clients at published API rates,
but it is not the owner's subscription bill. Price changes require a dated table update and
may reprice a live rollout. A rollout that cannot be read leaves its cost
unknown. The dispatch ledger applies the same provenance distinction to worker results.

Amended 2026-10-05: a Claude provider's per-attempt cost is the session's running total, so dispatch spend now adds only each session's rise over its last total (`dispatch/spend.py`). Summing the recorded totals had nearly doubled the real spend.

## Alternatives considered

Tokens everywhere would stay exact but would leave the two managers incomparable in dollars.
Applying one model's rate to `total_token_usage` would silently misprice a session that changed
models or crossed the long-context threshold. Treating an unpriced session as zero would make
missing evidence look free.
