---
id: 11
title: 'Harness state after the final audit: what ADRs 0001, 0005, 0006 and 0009 now read as'
status: accepted
date: 2026-09-15
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0011: Harness state after the final audit: what ADRs 0001, 0005, 0006 and 0009 now read as

## Context

The final audit found four accepted ADRs saying things the code no longer does, and one pair contradicting each other. [[adrs/0001-agent-repo-architecture|0001]] says workers run as `claude -p` and treats dispatch as future work. [[adrs/0005-mcp-tools-over-cli|0005]] names the deleted `migrate` command and a tool count of ~25. [[adrs/0006-dispatch-and-handoffs|0006]] says the supervisor auto-resumes at 1.5× budget, that parallelism is unlimited, and that `claude attach` has a `--bg` fallback. [[adrs/0009-agent-sdk-worker-transport|0009]] leaves "does the worker show in `claude agents`" open, and defers `can_use_tool` with no owner. Each is stale in one paragraph, not in its decision. Superseding any of them whole would be wrong.

## Decision

The four ADRs are read with these amendments; nothing else in them changes. 0001: workers run through the Agent SDK (0009). Dispatch is built. 0005: the Bash fallback is `doctor`, `obsidian`, `hook`, `repos sync`, `browser` and the streaming `dispatch` verbs. The manager profile serves 30 tools, against the ~35 ceiling. The single-implementation rule now holds on the CLI side too (the last ten handlers moved into `ops`). 0006: there is no auto-resume of a `question` and no automatic budget raise ([[adrs/0008-dispatch-policy-after-audit|0008]]). One dispatch per repo runs at a time, held by the `repo:<short>` lease, so parallelism is bounded by repo. `claude attach` and the `--bg` fallback were never built; the take-over line is `claude --resume <session>`. `dispatch say` is on the record's thread. 0009: the open item is answered by a probe (a worker is a listable, steerable peer). `can_use_tool` is adopted as a deny-only recorder and backstop behind the rule strings, with preflight's hard refusal kept.

## Consequences

- A reader of 0001, 0005, 0006 or 0009 also reads this one; each carries a pointer line.
- "Superseded in part" has no CLI mechanism (`--supersedes` is whole). This ADR is the mechanism; the rule in `.claude/rules/adrs.md` gains that case.
- The next audit checks ADR prose against the code the way this one did.

## Alternatives considered

- One superseding ADR per stale ADR: four records that each restate a decision that did not change, to correct a paragraph. Rejected for one amending record.
- Editing the four in place with dated notes: breaks the never-rewrite rule and leaves no single place that says what changed. Rejected.
