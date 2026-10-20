---
id: 59
title: 'Harness state after the launch doc audit: what seventeen older ADRs now read as'
status: accepted
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0059: Harness state after the launch doc audit: what seventeen older ADRs now read as

## Context

A documentation audit before launch read ADRs 0001-0049 against main. It found current-state sentences that are false and carry no pointer. Most are doctor rows cut under [[adrs/0058-harness-state-after-audit-5|ADR-0058]]; the rest are renamed modules, a moved file name and a changed config key. In each, one line went stale and the decision stands, so this is one amending record in the shape of [[adrs/0011-harness-state-after-final-audit|ADR-0011]] and [[adrs/0058-harness-state-after-audit-5|ADR-0058]].

## Decision

The seventeen ADRs are read with these amendments; nothing else in them changes.

- [[adrs/0002-python-over-rust|0002]]: "one runtime dependency (pyyaml)" reads: `pyproject.toml` lists `claude-agent-sdk`, `mcp` and `pyyaml`, plus `psutil` on Windows.
- [[adrs/0003-hooks-as-cli-subcommands|0003]]: `src/agent_cli/hooks.py` is the `hooks/` package, and `hooks/run.py` holds the `NAMES` table. `tests/test_hooks.py` is split across `tests/test_hook_*.py`, `tests/test_hooks_*.py` and others. "`agent doctor` checks the wiring matches" no longer holds.
- [[adrs/0004-obsidian-front-end|0004]]: "doctor checks it" no longer holds; doctor has no vault or generated-file check.
- [[adrs/0005-mcp-tools-over-cli|0005]]: `ops.py` and `cli.py` read as the `src/agent_cli/ops/` and `src/agent_cli/cli/` packages.
- [[adrs/0006-dispatch-and-handoffs|0006]]: the record is `features/NNN/handoffs/<tag>-SS-<short>.md` ([[adrs/0036-dispatch-id-machine-tag|ADR-0036]]). "A wikilink under the plan's `## Handoffs`" reads as a fixed Dataview view over the handoff files; dispatch writes no plan line ([[adrs/0039-plan-handoffs-are-a-view|ADR-0039]]).
- [[adrs/0009-agent-sdk-worker-transport|0009]]: "`doctor` reports both" no longer holds; the worker-transport row is gone. Version drift is still a fact to record, not a reason to use the bundled binary.
- [[adrs/0012-two-repos-harness-and-notes|0012]]: there is no tracked `harness/playbook.md`. Lessons go to the one local queue of [[adrs/0048-one-local-lesson-queue|ADR-0048]].
- [[adrs/0014-worker-repo-mcp-allowlist|0014]]: "When the key is absent, behaviour is unchanged and every repo server loads" reads: an absent, null or empty `repo_mcp` selects no product servers ([[adrs/0020-explicit-worker-mcp-grants|ADR-0020]]; `src/agent_cli/dispatch/mcp_policy.py`).
- [[adrs/0016-live-views-over-a-snapshot|0016]]: "`doctor` checks it" no longer holds; doctor has no Obsidian or Dataview row.
- [[adrs/0023-managers-work-in-worktrees-over-shared-state|0023]]: "and `doctor` says so" no longer holds; doctor does not flag a stale `STATUS.md`.
- [[adrs/0027-rubric-trusts-vendor-defaults|0027]]: `dispatch.model_from` is no longer read; `src/agent_cli/repos_validate.py` reports it, and `dispatch.model` pins a repo's model for Claude dispatches. "Doctor warns when a recorded default is over 30 days old or a new model appears behind an alias" no longer holds.
- [[adrs/0031-portable-hook-launcher|0031]]: `bin/agent-hook.cjs` runs no caveman hooks; they are gone (ADR-0058). Doctor has no hook-timing probe; the timing targets stand without a check.
- [[adrs/0035-cross-device-peers-over-restricted-ssh|0035]]: "enroll or doctor checks it" reads as enroll only. Enroll resolves the real versioned interpreter for the forced command; doctor has no peer-net row.
- [[adrs/0037-worktrees-under-one-granted-parent|0037]]: "Doctor checks each grant, and warns about a worktree still at the old path" no longer holds.
- [[adrs/0040-initiatives-nested-feature-containers|0040]]: no initiative move or instantiate tool exists in `src/agent_cli`. The line on typed move and instantiate tools describes a plan, not the code.
- [[adrs/0043-consumer-installs|0043]]: "Doctor shows the role and warns when local main is not an ancestor of `origin/main` or tracked files are modified" no longer holds. `agent land` and the pre-push hook still refuse on a consumer.
- [[adrs/0047-notes-only-landings|0047]]: the notes-only record is keyed by commit (`<sha>.json` in the notes-only store), not by tree; it also stores the tree (`src/agent_cli/notes_only.py`).

## Consequences

- A reader of any of the seventeen also reads this one; each carries a pointer line.
- Doctor's rows on main are those in `src/agent_cli/doctor/run.py`. An ADR line that promises a doctor row is checked against that list.

## Alternatives considered

- One amending ADR per old ADR: seventeen records that each restate an unchanged decision. Rejected for one record, as in ADR-0011 and ADR-0058.
