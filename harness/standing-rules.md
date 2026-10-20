---
tags: [doc]
---

# Standing-rule incidents

The owner controls the standing rules in [[AGENTS#Standing rules]]. This file preserves their
dates, reasons, and enforcement limits. Owner-specific rules live in `harness/owner/rules.md`.
Before changing a standing rule, read its incident here and show the owner the exact change.

## 2026-09-03 — never make assumptions

The owner said a wrong guess costs more than a question. `/intake` step 1 is a clarification
step that cannot be skipped. The always-loaded rule covers ambiguous instructions, tasks,
and plans even outside intake.

## 2026-09-03 — no chained shell commands

A chain hides which step ran and which failed, and makes the permission prompt unreadable.
`bin/agent hook no-chaining`, in `src/agent_cli/hooks/guards.py`, runs on both clients'
Bash-shaped calls inside their shell hook group (`pre-bash`, `codex-pre-bash`). Independent calls can share a client batch; dependent calls must wait for
the earlier result. The hook rejects additional shell-script constructs as explained in its
denial. Configuration and synthetic tests are not proof of all live wrapper coverage.

Relaxed later: it was the most frequent denial in the harness, mostly read-only pairs like
`grep …; wc -l …`. A chain of read-only commands from a
short allowlist with argument limits now passes (`src/agent_cli/hooks/readonly_chain.py`).
`git`, `rg`, `sed` and `find` stay out: each can write or run a program with no telltale flag
(`diff.external`, `rg --pre` behind quoting or a glob, `sed w`, `find -delete`), and any `$` takes
the chain out, since an expansion can become an option.

## 2026-09-03 — reviewable edits

A shell one-liner hides what changed and cannot be reviewed before it lands. Use visible
edit/patch tools, or the CLI for records it owns. Do not use `sed -i`, heredoc rewrites, or
`echo >` to edit files. This includes gitignored product configuration; the manager's product
edit boundary still applies. The generated-file shell guard covers only part of this rule.

## 2026-09-03 — never move features or ADRs in Obsidian

The CLI owns generated links and files. Obsidian link auto-update is off. A rename in the
explorer leaves links dangling, and there is no rename command. `alwaysUpdateLinks: false`
does not prevent a move, and nothing reports the dangling links it leaves (doctor's link check
was retired).

## 2026-09-03 — keep durable memory in the harness

The harness is the system of record and must be portable to other tools and teammates.
No auto-memory, memory files, or rules live in `~/.claude` or other tool-local storage.
Use logs, ADRs, or `harness/`. Claude sets `autoMemoryEnabled: false` and puts plan drafts
under `.agent/scratch/plans`; those settings do not establish the same boundary in Codex.

## 2026-09-04 — route design artifacts

The manager built a design artifact itself instead of routing it. Mockups, design specs, and UI artifacts belong to a designer or worker. The manager writes
the work order and publishes the result through the authorized workflow. `/dispatch` is the route.

## 2026-09-04 — pull before reading a product clone

The manager searched a stale product clone and said a skill merged the previous day did not
exist, and the plan then depended on other stale facts. Freshen every clone before research each session;
report the read commit when relevant. `repos sync --pull` records freshness. `pull-warn` is
advisory and wired only in the Claude config. Dispatch separately fetches before branching.

## 2026-09-03 — route review feedback

“Take a look at the review” once led the manager to work
through every finding with worse context than the implementer. Read only enough to route:
repo, branch, and anything the owner must decide. Send the worker to the thread through
`/dispatch`; it owns the judgment and fixes. Workers cannot speak on GitHub. The manager
publishes or resolves only when asked, and verifies fixes on the pushed head before resolving.

## 2026-09-24 — reviewed changes may commit locally

The owner asked for the implementation to be driven and reviewed to approval, but never
asked for commits. The Codex implementer made six local commits; the Claude reviewer told it
to commit three times. The owner chose this rule over asking first for every commit:
an owner-paired implementer may commit after its reviewer approves the staged diff, staging
only the feature's named paths. When reviewed work is ready, ask once with `AskUserQuestion`
(or `owner_question` on Codex): push and merge `<branch>` now? On yes, run it yourself. Hand
over a command only if the client refuses, and say why. The yes authorizes publication; the
push hook checks review state and does not authorize manager pushes.

Reworded later: manager pairing was removed, and the small-change route gives the same case a
fresh cross-client reviewer. A manager may commit its own small change on a branch once that
reviewer approves the diff. Amended again: ask once, then publish within that authorization; do not relay commands by default.

## 2026-09-24 — check client-specific recommendations

A Claude manager offered the owner a recommended Codex plan-gate
option before asking the Codex manager how Codex presents plans. The owner said a recommendation
about a Codex- or Claude-specific feature should be run by a manager of that client first.
Ask that manager before recommending client-specific behavior to the owner. This needs judgment
at the recommendation point; no hook can reliably classify every recommendation.

## 2026-10-05 — plan approval covers the plan's routine asks

The owner's approval of a plan
covers the routine manager-to-owner asks that plan already describes: the manager does not ask
again for each step it lists. It covers nothing else. A destructive, external, permission or scope
change still needs the owner's explicit, possibly bounded, authorization at that moment, and the
approval never widens what a worker may do: worker containment stays deferred, and the worker
allowlist gained only commands that cannot write. This is a prose rule; nothing checks it.

Keep each rule's operative text in AGENTS.md: Codex does not expand imports, so a rule that
lives only here never reaches it (ADR-0024).

## Which hooks a user can switch off

A user switches a hook off in `agent.yaml` or `agent.local.yaml` (the local file wins), for
example `hooks: {thrash-guard: false}`. The syntax is in [[harness/agent-yaml]].

Why: the removable hooks are process aids; the rest keep the standing rules and safety boundaries enforced.

The removable hooks are listed in [[harness/agent-yaml]] (`TOGGLEABLE` in
`src/agent_cli/hooks/toggles.py`).

Protected core (every name in `HANDLERS` in `src/agent_cli/hooks/run.py` that is not removable): `session-start`,
`capture`, `protect-generated`, `no-chaining`, `verify-pipe`, `push-guard`, `commit-guard`,
`pattern-kill-guard`, `sandbox-help-guard`, `subagent-revert-guard`,
`render-after-plan-edit`, `dispatch-inbox`,
`manager-prompt`, `codex-prompt`, `worker-stop`, `precompact`, `lease-yield`, `session-name`,
`activity`, `codex-activity`, `session-reap`, `peer-stop`, `cli-route`, `plan-mode-allow`,
`notes-sync`.

A leftover switch for a retired hook (`toggles.RETIRED`) is ignored. Any other name outside the
removable list, or a value that is not true or false, is a config error that stops the config
from loading, since it would void every switch in both files.
