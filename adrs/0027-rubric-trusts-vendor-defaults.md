---
id: 27
title: The dispatch rubric names models and trusts vendor effort defaults
status: accepted
date: 2026-09-24
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0027: The dispatch rubric names models and trusts vendor effort defaults

## Context

The rubric pinned an effort level on every row. Vendors change defaults: Opus 5.5 ships at
`medium`, and the `build` row kept sending `high`. The Codex half was a second pinned table.
Anthropic and OpenAI both document their defaults and when to leave them, and both say to
measure rather than carry settings over.

## Decision

Rubric rows name a model per provider and carry no effort; a dispatch omits effort, so the
model runs at its vendor default. A named override (`long-run`, `trivial`, `deep-review`)
leaves the default only where a vendor source says when, and cites it in the choice reason.
Claude defaults are recorded with a source and check date, because `GET /v1/models` lists
supported levels but not the default. Codex defaults are read live from app-server
`model/list`. Rows name aliases and follow them; each dispatch records the model id it ran,
and an id without a recorded default is flagged. A repo's own model preference
(`dispatch.model_from`) is read at every dispatch and beats the row. Escalation steps the
model rung and raises effort only at the top rung, capped at `xhigh`; it never reaches the
owner-only `max` row.

## Consequences

[[adrs/0049-codex-row-effort-policy|ADR-0049]] amends the no-row-effort rule for Codex review and build.

Effort drops for Opus rows (from `high`/`xhigh` to Opus 5.5's `medium`), which is the token
saving the owner asked for; a regression shows in calibration and is answered with an
override, not a pin. Doctor warns when a recorded default is over 30 days old or a new model
appears behind an alias. User-level `modelSettings` in `~/.claude/settings.json` still apply
to workers, because `setting_sources` is unset; today they equal the vendor defaults.
Codex rows stay unselectable until the Codex transport passes its launch gate.

Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: `dispatch.model_from` is no longer read (`dispatch.model` pins a model), and doctor has no model-default age row.

## Alternatives considered

Keep per-row effort and re-pin on each vendor change: that is the staleness this removes.
Scrape vendor docs on a schedule: brittle, and it would silently change behavior. Pin model
snapshots instead of aliases: the owner chose to follow aliases and be told.
