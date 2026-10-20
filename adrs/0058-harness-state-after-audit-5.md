---
id: 58
title: 'Harness state after audit 5: what eleven older ADRs now read as'
status: accepted
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0058: Harness state after audit 5: what eleven older ADRs now read as

## Context

A harness audit deleted modes, guards, doctor rows and shims with no observed consumer. Eleven accepted ADRs still describe some of them. In each, one paragraph went stale and the decision stands, so this is one amending record in the shape of [[adrs/0011-harness-state-after-final-audit|ADR-0011]]. ADRs whose whole decision went are superseded separately ([[adrs/0056-retire-ledger-and-scheduler|ADR-0056]], [[adrs/0057-retire-telemetry-report|ADR-0057]]).

## Decision

The eleven ADRs are read with these amendments; nothing else in them changes.

- [[adrs/0005-mcp-tools-over-cli|0005]]: "mcp_server.py tools" reads as `src/agent_cli/mcp/server.py`; the `mcp_server.py` re-export shim is gone.
- [[adrs/0006-dispatch-and-handoffs|0006]]: "chat, macOS, or a slack webhook when they are away" reads as chat, plus macOS on a Mac. `agent away`, per-dispatch channels and `dispatch notifications-retry` are gone; the supervisor's reconciler still retries a stranded notice.
- [[adrs/0012-two-repos-harness-and-notes|0012]]: "the eval work orders" left the private set with the eval runner. Rubric calibration rows are no longer written at dispatch end; `/retro` appends them.
- [[adrs/0015-live-workers-and-visible-sessions|0015]]: "the holder is nudged and yields at turn end" reads: a denied edit still records a request; `lease-yield` stamps the turn end, the lease reads resting, and a waiter's next edit takes it. `lease-nudge` is gone from "New hooks".
- [[adrs/0023-managers-work-in-worktrees-over-shared-state|0023]]: worktree isolation is now the only guard against a broken edit reaching other sessions; `syntax-guard` is retired. "`doctor` fails — not warns — when a worktree's `.agent` is a real directory" no longer holds: `worktree list` reports the shared-state link, and doctor does not check it.
- [[adrs/0024-findings-are-tracked-until-they-earn-a-home|0024]]: "`CLAUDE.md` is deleted rather than shimmed" is held by doctor's project-instructions check and `tests/test_doctor_instructions.py`; `instruction-name-guard` is retired.
- [[adrs/0025-caveman-lite-held-not-hoped|0025]]: the enforcing half is retired. `style-guard`, `style.TIC_PATTERNS`, `agent style drift` and the repo's vendor caveman hooks are gone; the `caveman-lite` output style and `.caveman.json` remain.
- [[adrs/0030-agent-yaml-config|0030]]: the transition rules are void. The `repos.yaml` reader and `notes migrate-config` are deleted; `repos.yaml` stays in `.gitignore` and the export's never-list as private config. "A test fails when a key the code reads is missing there" no longer holds.
- [[adrs/0033-models-reach-the-cli-through-mcp|0033]]: `cli-route` still enforces the rule. The `harness` tool no longer has `eval` or `style` actions. "Tests fail when a CLI command is in neither map or a CLI option has no tool parameter" no longer holds; `mcp/coverage.py` stays for `cli-route` and `mcp_wording`.
- [[adrs/0036-dispatch-id-machine-tag|0036]]: doctor no longer warns on a device-name tag or reports labels, caches or handoff conflicts; `dispatch new` still refuses an unset or foreign label.
- [[adrs/0045-full-suite-only-at-landing|0045]]: selection policy 1 and the `selection-migration` run are deleted. A dispatch not on policy 2 runs its verify commands as configured. Under evidence from policy 1, a full row stands and a scoped row is reverified.

## Consequences

- A reader of any of the eleven also reads this one; each carries a pointer line.
- Historical mentions of `repos.yaml` in ADRs 0001, 0010, 0013 and 0014 describe what was true then and stay.

## Alternatives considered

- One amending ADR per old ADR: eleven records that each restate an unchanged decision. Rejected for one record, as in ADR-0011.
