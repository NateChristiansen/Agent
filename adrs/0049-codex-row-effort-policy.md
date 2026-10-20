---
id: 49
title: Codex review and build carry an owner effort policy
status: accepted
date: 2026-10-09
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0049: Codex review and build carry an owner effort policy

## Context

Installed Codex currently recommends `low` for Sol 6.1. The owner chose `medium` for
Codex review and build. That is a task-routing policy, separate from a vendor default.

## Decision

Codex review and build carry an owner-selected `medium` effort policy. Other rows omit
effort. Explicit effort, named overrides and inherited parent effort take precedence.
Escalation preserves the parent's recorded effort, including omission. Recheck row policy
with the owner when changing the row's model; do not treat it as a cached Codex default.

This amends ADR-0027's “Rubric rows name a model per provider and carry no effort; a dispatch
omits effort, so the model runs at its vendor default.” Read it as: rows normally omit effort;
the two Codex row policies above are the owner's explicit exception. Codex vendor defaults
still come from live `model/list` and are never hard-coded into the rubric.

## Consequences

Preview, record and worker options name `row-policy` as the effort source. Explicit effort,
overrides and inherited parent effort remain distinguishable. Escalating Luna to Sol with
omitted parent effort still uses Codex's default. Model integration must check both vendor
metadata and owner routing decisions. Claude rows retain vendor-default behavior.

## Alternatives considered

Use Sol 6.1's low default: the owner chose medium for these task shapes.
Pin medium per model: that would lose the owner's row policy when a row's model changes.
Hard-code Codex defaults: account metadata already supplies them live.
