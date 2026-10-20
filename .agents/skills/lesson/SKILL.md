---
name: lesson
description: "Turn a mistake into a tracked finding, a binding mechanism, or justified skill prose at the decision point. Use when the owner says something was a mistake, when you are corrected, or when you notice a wasted loop."
user-invocable: true
allowed-tools: mcp__agent__*, Read, Edit, Write, Grep, Glob, AskUserQuestion, Bash(uv run pytest:*), Bash(uv run ruff:*), Bash(uv run pyright:*)
---

# /lesson

Read `references/lessons.md` before routing or recording a finding.

Input: $ARGUMENTS (the owner's words about what went wrong) or the last few turns. Output: a
recorded finding with an owner and verifiable end, a binding fix, or justified prose in an
existing skill (ADR-0024). Do not skip step 1. The owner controls standing rules.

`/lesson review` is a separate mode: go to step 7.

## 1. Restate the mistake

Two sentences: what happened, and what it cost (turns, dollars, a wrong fact, a stranded
commit). Point at the evidence: a log entry, a handoff, a dispatch id, a transcript path. If
you cannot point at evidence, say so and ask the owner before recording anything.

## 2. Where the fix belongs

You decide this; the owner does not tag it for you. Read the restated mistake and its evidence and
apply one test: would the finding make sense, verbatim, in a stranger's clone of the public
harness (ADR-0012)?

- Yes → `manager`. It is about how the manager, workers, hooks, CLI, dispatch, budgets, models
  or records behave. The fix or tracked task lands in this harness. Repo shorts or PR numbers
  used only as evidence pointers do not change this.
- No, because it only holds here: it names a private system (an internal service or tool), a
  local convention, a ticket format, or spans repos whose harness is not
  the owner's → `owner`. Track the finding in the private feature. A change in how the owner
  themselves works may go to `harness/owner/ai-native-developer.md` as a practice with the
  example, after the owner accepts it.
- No, because it is one repo's commands, conventions, lint or review bar, setup or tooling →
  `repo:<short>`. The fix is that repo's CLAUDE.md, skills, hooks, or its `dispatch:` block in
  `agent.yaml`. The agent.yaml part you do here; anything inside the repo is a dispatch
  (`/dispatch`), never an edit from this session. Keep the task in the feature until the repo
  adopts it.

Say which branch you took and quote the evidence that decided it. The owner overrides by saying
so; then record their choice and reason.

## 3. How is it held

Triaged findings have three states (ADR-0024):

- **Tracked**, the default. Where it goes depends on whether the finding has an open feature:
  - It came from an open feature: add a task to that feature with a named owner and verifiable
    end, or an open question requiring a decision.
  - It came from a closed feature, a retro, or no feature: queue it (ADR-0028). A task on a
    closed feature would reopen it. Use the `lesson` tool with `action: "add"`. Every install
    keeps one local queue in its notes repo (ADR-0048); `scope` is ignored. Write the evidence
    so it states the symptom or the owner's quote on its own, never "see feature NNN":
    a `manager` lesson may later become a public issue, and step 7 rewrites it for a stranger then.
  An explicit drop names why the finding is obsolete or redundant. Do not turn an unowned
  proposal into prose.
- **Mechanism**, preferred when the failure can be decided from a tool call, diff or file:
  implement a hook, CLI validation, permission boundary or regression test. Verify that it is
  wired and that failure blocks the action or fails the test. Put the incident in the
  mechanism's docstring, test name or deny message. A warning-only hook is advisory; a
  recorded bypass means the boundary is not enforced.
- **Prose**, only when judgment cannot be reduced to a check and an existing skill loads at
  the decision point. Put the concrete rule and its incident evidence in that skill's
  `references/lessons.md`, and make the SKILL.md explicitly read the reference. If no skill
  loads there, keep the finding tracked.

If a fix covers only part of a finding, name the covered part and track the rest.

## 4. Record

Record the incident and disposition in the current feature log, or, for a queued finding,
the queue entry is the record. Add a task before work starts if the finding is tracked in a
feature. A public mechanism or skill reference carries its own evidence:
name the number, command or owner quote that makes the symptom checkable, not a private log,
dispatch, transcript or feature wikilink. Do not give a new finding a `Pxxx` id. The
numbered series ended with the playbook; ids still in the tree are inert tags kept next to the
mechanism they belong to, and there is no index that resolves them.

## 5. Apply

- `manager` + hook/CLI/test: make the change now, with a test, and run the tests it touches
  (`uv run pytest tests/test_x.py`); the full suite runs at landing.
- `manager` + skill: edit the skill and its reference now. Keep only the rule and evidence it
  needs at that decision point.
- `repo:<short>` inside the repo: track the finding as a feature task, write the work order
  and run `/dispatch`. Close the task only after the handoff verifies the fix.
- Anything that widens a permission or changes a rule the owner owns: show them the exact
  change and wait.

## 6. Report

Three lines: where the finding landed (a feature, or a queue id), what changed (file and
function), and what remains tracked with its owner and verifiable end.

## 7. Review the queue (`/lesson review`)

Run this when the session-start block says the queue holds 10 or more, when `feature close`
refuses on the queue, or when the owner asks.

1. List it: the `lesson` tool with `action: "list"`. On a source install (`harness.role` is not
   `consumer`), also list the open upstream issues that carry a lesson marker, as the owner's
   inbox:

   ```sh
   gh issue list --repo <upstream> --state open --search '"harness-lesson:" in:body' --limit 500 --json number,title,url
   ```

   `<upstream>` is the `upstream` that `action: "draft_check"` returns. GitHub search stops at
   1,000 results; when the count equals the limit, use
   `gh api --paginate "repos/<upstream>/issues?state=open"` instead, keep bodies that contain
   `harness-lesson:`, and drop pull requests. Triage each issue like a queue entry.
2. Retirement check first: for each open entry, look for a later commit or mechanism that already
   answers it (search the code and `git log` for what its `home` names, and the resolved
   entries for a duplicate). An entry a commit retired is resolved `mechanism`, or `dropped` when
   the change removed the thing it was about, naming that commit; do not carry it as open.
3. For each entry still open, propose where it goes, with a one-line reason:
   - `upstream`: a harness lesson that holds in a stranger's clone (step 2's `manager` branch)
     and that this install does not fix itself. File it as an issue with the steps below.
   - `config`: the fix is a dispatch or repo configuration change, such as an `agent.yaml` block.
   - `repo`: it belongs to a product repo's own docs or rules; dispatch it there.
   - `task`: work that needs a feature. Add it to an open feature whose scope matches, or
     propose one new harness feature for the batch.
   - `mechanism` or `prose`: small enough to apply now, with step 3's rules.
   - `practice`: a change in how the owner works, for `harness/owner/ai-native-developer.md`.
   - `dropped`: obsolete or already covered; name what covers it.
4. Ask the owner about each entry on its own: one question per entry, with your proposal as the
   recommended option. Never offer a decision for the whole batch; the owner always decides one
   by one. A `practice` is added only on the owner's yes.
5. Apply each decision. Then resolve the entry with `action: "resolve"`, a `disposition`, and
   a `note` naming the task (`<feature>#<task>`), the commit, the file, the issue URL, or the reason for
   dropping.
6. Report: resolved count by disposition, and what is still open.

### Filing an `upstream` entry

1. Search for a duplicate:
   `gh issue list --repo <upstream> --state all --search "<key words> in:title" --limit 50`.
   Show any match to the user, who may comment on it instead of filing. The search is a
   courtesy check, not proof that no duplicate exists.
2. Write the issue body to a scratch file (the `harness` tool with action `scratch_path`). It
   holds the body only, and ends with the line `harness-lesson: <id>`, the queue entry's id.
   The title is a separate string that starts with `[lesson]`. Write both for a stranger: the
   symptom, the cost and the proposed fix, with no private name, repo, ticket or path.
3. Call the `lesson` tool with `action: "draft_check"`, `title` and `body_file`. It returns the
   `upstream` repo and a fallback URL. It refuses a non-GitHub origin; say so and stop.
4. Show the user the full title, the full body and the `upstream` repo, and wait for a yes.
5. On the yes, run `gh issue create --repo <upstream> --title "<title>" --body-file <body_file>`.
   Use no label: GitHub drops labels set by anyone without push access, so the marker line is
   what finds these issues. When `parent_lookup` is `unavailable` (no `gh`, or not logged in),
   file nothing: give the user `fallback_url` to open. If `truncated` is true, tell them the
   body was cut and to paste the rest from the body file.
6. Resolve the entry with disposition `upstream` and the issue URL as the note. If the resolve
   fails, the URL is still in this chat: retry the resolve, never the create.
