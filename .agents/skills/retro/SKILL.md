---
name: retro
description: "Review a finished feature's runs (log, handoffs, dispatch records, prompts, transcripts) with a lower-tier subagent, verify the numbers against the records, write features/NNN/retro.md, append rubric calibration rows, and route findings through /lesson. Use when a feature closes (/close runs it first), when a closed feature lacks a retro, or when the owner asks to review a session."
user-invocable: true
allowed-tools: mcp__agent__*, Bash(jq:*), Bash(ls:*), Bash(grep:*), Bash(wc:*), Read, Write, Edit, Agent, AskUserQuestion
---

# /retro

Read `references/lessons.md` before gathering records or writing calibration rows.

Input: $ARGUMENTS, a feature ref (`5`, `005`, a slug) or `harness` for a cross-feature pass.
Output: `features/NNN-slug/retro.md` (or `harness/owner/retro-YYYY-MM-DD.md` for `harness`),
new rows in `harness/owner/calibration.md`, one `measurement` log entry, and
zero or more findings routed through `/lesson`. A retro is hindsight for the owner in
six months:
what it cost, what hurt, what worked, what changes.

## 0. Gather, do not read

With no feature ref, use `feature_list` with `include_closed: true`, list closed features whose
folders lack `retro.md`, and ask which to review.

List the material and its sizes; the reading is the subagent's job (step 2):

- `features/NNN-slug/plan.md`, `log.md`, `handoffs/*.md`, `retro.md` (must not exist yet; if
  it does, ask whether to overwrite).
- Dispatch records: `jq -c '{id, model, effort, budget_usd, cost_usd, attempt, state,
  num_turns, auto_resumes}' .agent/dispatches/NNN-*.json` — this is the ledger and you keep it.
- Prompts and transcript paths: `.agent/raw/NNN-slug/*.jsonl` (`kind`, `prompt`,
  `transcript_path`). If the feature is still open, use the `raw_preserve` tool first so
  the transcripts survive Claude Code's 30-day cleanup; `feature_close` does it on its own.
- Current feature tasks and `harness/dispatch-rubric.md`.

## 1. The question set

The subagent answers exactly these, with evidence (file, timestamp, dispatch id, quote):

1. Ledger: per dispatch, what was asked, what came back, cost vs budget, attempts, model and
   effort, and what went wrong if anything.
2. Thrash: anything attempted more than once, resumed more than once, rewritten, or a long
   stretch of tool calls that produced nothing that survived. Who caused it: the owner's
   prompt, the manager's assumption, a harness bug, the environment.
3. Corrections: every time the owner corrected the manager, quoted, and whether the lesson was
   recorded in the feature log, an accepted task, a mechanism, or loaded skill prose.
4. Denials and errors: permission denials, hook denials, tool errors, and whether any repeated.
5. Rules broken after being written: a mechanism or standing rule whose symptom appears again
   later in this feature.
6. Model and effort: what ran on what, and whether a cheaper or a stronger choice was
   indicated by the outcome (be specific, or say there is no evidence).
7. The owner's prompts: which ones led to clean single-pass outcomes and which to repair, with the
   pattern named (bundled asks, no done-criteria, one-word approval over an open question,
   "take a look" phrasing, a session left running across features).

## 2. Delegate the reading

One `Agent` call, `subagent_type: retro-reader` (`.claude/agents/retro-reader.md`: sonnet,
read-only tools, the seven questions, the jq filters and the Bash rules as its system prompt,
and a JSON answer with one key per question). Pass `model: haiku` for a feature with one
dispatch and under 100 prompts. The prompt carries only the facts: the feature, the file list
from step 0, the ledger you extracted (it keeps those numbers), and anything specific to check
(a defect found later, a claim in the log). The reply is one JSON object; read it as data.
Fallback if the definition is missing: `subagent_type: general-purpose`, `model: sonnet`, with
the definition's body pasted as the brief.

## 3. Verify before you trust

Before writing anything, check the claims that change a conclusion against the records
yourself: the ledger numbers against the jq output from step 0, a quoted correction against
`grep` on the raw jsonl, a "rule broken again" against the mechanism's install date. Say in the
retro which claims were verified and which are the subagent's reading: a lower-tier reading
is a draft, not a finding.

## 4. Write retro.md

Frontmatter: `tags: [retro]`, `links: ["[[features/NNN-slug/plan|NNN slug]]"]`, `feature: NNN`,
`date`. Sections, in order, each short:

- **Ledger**: the table from step 0 with totals (spend, attempts, dispatches that needed the owner).
- **What hurt**: the thrash and denials, each tagged with a cause from `/lesson` step 2
  (`manager`, `repo:<short>`, `owner`, `harness-bug`) and the dollars or turns it cost.
- **What worked**: what to keep doing, with the evidence.
- **Lessons**: one line per finding with its proposed home (or the mechanism that already covers it) and its
  queue id once step 5 queues it, and one line per owner practice.
- **Model and effort**: the calibration rows you are about to append, and what they suggest.

## 5. Record

- Append the calibration rows to `harness/owner/calibration.md` (one per finished dispatch:
  dispatch, row, model, effort, budget, cost, attempts, outcome, note). The rubric they
  calibrate is `harness/dispatch-rubric.md`; the rows are private (ADR-0012). Nothing writes a
  row when a dispatch ends; this step is where the rows come from. Put your conclusion about
  the effort choice in Note.
- Check the rows that are already there for this feature against the dispatch records before
  appending new ones, and rewrite any that disagree. A row written while its dispatch was still
  running records that moment; a resume, an overrun or an abandonment changes every column, and
  a stale row is evidence the rubric is read as true.
- `log` tool, kind `measurement`, with `feature: NNN` named explicitly: total spend,
  dispatches, attempts, corrections, and the retro path. One entry. The session may be mapped
  to a different feature than the one being reviewed; a log call without `feature` lands
  wherever the session points (it has landed in another feature's log).
- Queue every proposed lesson and owner practice (ADR-0028): the retro's feature is closing,
  so a task there would reopen it. Apply `/lesson` step 2's test to pick the scope (`harness`
  if it would hold in a stranger's clone, `private` if it names a private system, a
  local convention, one repo, or is an owner practice), then add it with the `lesson` tool,
  `action: "add"`, kind `lesson` or `practice`. Public evidence states its own symptom.
  Do not ask a yes per item here; `/lesson review` is where the owner decides. Show the owner
  each queued entry with its id in the report. A practice reaches
  `harness/owner/ai-native-developer.md` only when accepted at review.

## 6. `harness` mode

Same steps over every feature closed since the newest `features/*/retro.md` (or all of them if
none exist), plus changed mechanisms and feature tasks for the period.
The subagent's questions gain one: which tracked findings found a home, and which installed
mechanisms had their symptom recur. Output goes to `harness/owner/retro-YYYY-MM-DD.md`.

## 7. Report

Three lines: the retro path, the totals, and how many lessons were queued, with their ids.
