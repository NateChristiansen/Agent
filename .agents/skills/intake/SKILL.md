---
name: intake
description: "Turn a prompt, pasted ticket, error, or question into a tracked feature: clarify with the owner first, then feature_new + feature_use, write the plan.md body, log the first entry. Use whenever a task arrives that has no feature in Dashboard.md."
user-invocable: true
allowed-tools: mcp__agent__*, Read, Edit, Write, Glob, Grep, AskUserQuestion
---

# /intake

Input: whatever the owner gave you ($ARGUMENTS or the last message). Output: a mapped feature
with a plan body and one log entry. Do not skip step 1.
Read `references/lessons.md` before clarifying or writing the plan.

## 0. If it is a ticket, read the ticket

Applies when the input is a ticket id (`T-123`, `JIRA-45`), a bare id, or a ticket URL, and a
tracker's MCP server is configured and enabled here (ADR-0013). Fetch the ticket with that
server's read tool before anything else. Pass every scoping argument the tool takes (a project,
a workspace): a server that has to ask for one elicits a form, and a headless caller cannot
answer. When no tracker server is configured, ask the owner to paste the ticket.

Take from it: the title (as the basis for `title`, not verbatim if it reads like a tracker's
phrasing), the description and acceptance criteria (as the raw material for Context), the state,
and the id itself for `tickets:`. Note the parent if it has one.

Then go to step 1 anyway. The ticket says *what*; it does not say which repos, what done looks
like here, or where this sits against Dashboard.md. A fetched ticket shortens the questions, it
does not remove them.

## 1. Clarify

Restate the task in two sentences. Then ask, in one numbered list, everything you are not sure
of. Typical gaps: which repo(s), the ticket id if step 0 did not supply one, what "done" looks
like, priority relative to Dashboard.md, whether this is a new feature or belongs to an open one,
and **who touches the artifact**: the manager (planning, research, records), a worker (any
code, mockup, or review inside a product repo), or the owner. "Take a look at", "build", and
"help me understand" have each ended with the manager doing worker-shaped work; when the answer is a worker, say so in the plan and route through `/dispatch`. A small
harness change (about 50 non-test lines, no hooks, permissions, land, verify, push or peer-net
code) needs no new feature or dispatch: add it as a task on an existing feature and use the
small-change route in AGENTS.md (manager edits, targeted checks, one fresh cross-client review,
`land`). For
every team-process step you would put in the plan (a design review, QA, rollout, a handoff),
check whether it applies to this work first. Cite the precedent: what the team's last comparable
efforts did for that step, and whether this work reuses something that already passed it
(a shared component, an existing flag). A step with no precedent is a question for the owner,
not a task. An owner naming who will build something assigns ownership; it is not
a go-ahead. Before creating a feature or editing anything, ask whether to start now or wait for
a plan (references/lessons.md, "ownership is not a go-ahead"). Wait for answers. If assigned work
matches an open feature, call `feature_use`. Inspecting, answering or cleaning up another
feature's worker keeps the mapping: use explicit `feature`/`dispatch_id` arguments with
`dispatch_read` or `dispatch`. A peer message does not assign ownership. Mapping from one,
or switching away from open work, needs the owner's ask and `switch=true`.

## 2. Create and map

- Check every repo you will name in `repos` against `repos_list` first; `feature_new` fails
  with "unknown repo" on one that is not registered. For each unregistered repo, ask the owner
  one question: register it now (recommended) or leave it off the feature. A repo with no
  remote is supported: register it without `remote`.
- The repo procedure (`agent-config` skill, `references/repos.md`) writes an intake record and
  runs a dry run under a feature, so the feature comes first. Call `feature_new` with the
  registered repos only. Then follow the procedure for each repo the owner agreed to register,
  and add it with the `feature_set` tool's `add_repo`. Never pass an unregistered repo to
  `feature_new`.
- State one owning manager for the feature in the plan Context. Keep the feature small: a few
  nodes. Split larger work into separate features.
- `feature_new` tool: `slug`, `title`, optional `tickets`, `repos`. The slug is kebab-case, two
  to four words. A ticket id goes in `tickets`, not the slug. `use` defaults to true, so the
  session is mapped in the same call.

## 3. Write the plan body

Tasks go in through the `task_add` tool so they are numbered from the start, one per task,
each a verifiable outcome.

Edit `features/NNN-slug/plan.md` below the frontmatter. `## Context` is three to six lines in
the owner's voice: what and why. `## Tasks` is a checkbox list, each one verifiable. Write it as
a dependency graph, not a sequence: give each task its prerequisites and the paths it writes, so
everything whose prerequisites are met runs at once, in waves. A task waits for another only when
it needs that task's landed result or writes the same paths; "do these in order" is never a reason
on its own, because a serial chain leaves work idle that could fan out.
`## Open questions` holds anything still unanswered; keep it until it is empty.
`## Verification` says how we will know it is done. Leave the other headings in place. Anything
the Context cites inside the vault (a prior feature, a design note, an ADR) is a wikilink in
the body and a line in the frontmatter `links:` list, so the graph and the Dashboard board see
it; the CLI adds ADR links itself when `adr_new` runs.

## 4. First log entry

`log` tool, kind `decision`: "opened: <one line on scope and the first move>".

## 5. Report

Three lines to the owner: feature id and path, what you will do next, what you still need from
them.
