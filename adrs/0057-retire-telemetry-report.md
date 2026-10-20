---
id: 57
title: Retire the fixed-question telemetry report
status: accepted
date: 2026-10-12
supersedes: 46
superseded_by: null
tags:
- adr
links:
- '[[adrs/0046-telemetry-report-fixed-questions|ADR-0046]]'
---

# ADR-0057: Retire the fixed-question telemetry report

## Context

[[adrs/0046-telemetry-report-fixed-questions|ADR-0046]] proposed one read-only `report` command
answering five fixed questions, with `workflow_record` facts and per-landing assessments as its
new records. It stayed `proposed` and was never accepted. The `report` tool was almost never
called. `workflow_record` and the assessments under `.agent/land/assessments` existed only to
feed it.

## Decision

The report, the workflow-quality facts and the landing assessments are retired in audit 5;
ADR-0046 is superseded and its proposal is closed as not adopted.

## Consequences

- Deleted: `src/agent_cli/report/`, `telemetry/workflow.py`, `land_assessment.py`, and the
  `report` and `workflow record` CLI, ops and MCP specs.
- Kept: telemetry events ([[adrs/0038-telemetry-local-jsonl-sink|ADR-0038]]), owner-question and
  attention events, and the land follow-up notices. Worker spend per day stays in the journal.

## Alternatives considered

- Accepting ADR-0046 as built: a handful of calls do not pay for thousands of lines of code and
  tests.
