---
id: 46
title: One read-only report answers fixed questions from existing records, with provenance and coverage on every figure
status: superseded
date: 2026-10-07
supersedes: null
superseded_by: 57
tags:
- adr
links:
- '[[adrs/0057-retire-telemetry-report|ADR-0057]]'
---

# ADR-0046: One read-only report answers fixed questions from existing records, with provenance and coverage on every figure

## Context

The harness writes dispatch records, telemetry events (ADR-0038), land journals and pass records
(ADR-0041), but no one report reads them with consistent provenance. Ad hoc reads can
double-count events or include quarantined test fixtures.

## Decision

The harness has one read-only `report` command, CLI `agent report` and MCP tool `report`, that
answers five fixed questions: time from launch to landed, cost, test-selection health
(ADR-0045), quality proxies, and suite health.

- Every figure carries its source record kinds and identities, its `[since, until)` UTC window,
  and a coverage of `complete`, `partial` or `inferred`. Missing data is blank with a reason. A
  partial subtotal is never shown as a complete zero.
- Event reads apply the exclusion manifest and dedupe by event id. An existing manifest that
  cannot be read or validated makes event-derived figures missing, not unfiltered.
- Ambiguous joins stay unresolved. Measured, estimated, unknown and legacy cost stay in separate
  columns. Intervals that overlap are never shown as an additive split.
- A question gets a new record only when existing records cannot answer it, and the change that
  adds it says why. The first such records are optional context on selection rows, one landing
  assessment per land request, and a narrow `workflow_record` writer for three typed facts
  (review leading to rework, a post-landing fix or revert, an attested owner intervention) in
  the existing `workflow.*` event namespace.

## Consequences

- The report never writes, migrates or backfills records. Its output fits a manager's context.
- Quality relation figures without recorded `workflow_record` links read as untracked, not
  zero. Owner questions asked without the `owner_question` tool are outside the count, and the report says so.

## Alternatives considered

- A general telemetry platform or central store: more than five questions need, and data
  leaves the machine.
- Classifying free text or prompts for findings and owner actions: guesses shown as counts.
