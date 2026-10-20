---
name: journal
description: "Write or refresh the generated block of one or more daily notes in journal/: a short bullet list of what got done that day, in the owner's voice, drafted by a sonnet subagent from the day's records, verified against them, written between the markers, published by notes sync (with it off, committed as `journal: <date>`). Run after a feature closes, when the journal nudge lists milestones, or on request: `/journal` (today), `/journal <date> ...`, `/journal yesterday today`, or `/journal missing` when the session-start block lists days without a block."
user-invocable: true
allowed-tools: mcp__agent__*, Bash(git add:*), Bash(git commit:*), Bash(git status:*), Bash(git diff:*), Bash(git -C:*), Bash(jq:*), Bash(ls:*), Bash(wc:*), Bash(grep:*), Bash(cut:*), Bash(mkdir:*), Read, Write, Agent
---

# /journal

Input: $ARGUMENTS, zero or more of: a date (`2026-09-09`), `today`, `yesterday`, `missing`.
Nothing means `today`. Output: for each day, the block between `<!-- journal:generated -->`
and `<!-- /journal:generated -->` in `journal/YYYY/MM-Month/DD-Weekday.md` rewritten, the note's `spend_usd`
and `dispatches` properties set from the usage records by the CLI, published by notes sync at
turn end (with `notes_sync` off, one commit `journal: <date>` per day that changed), and a
three-line report. `harness/journal.md` says
what the note is. `## Notes` is never touched here: it is the owner's.

The block is what the owner would type into a standup about that day: a handful of plain bullets,
one thing done per line. It is drafted by a subagent from the records and checked against
them before it is written; nothing in it comes from memory, and nothing in it is prose.

## 0. Resolve the days

- Only the journal machine writes (`journal.machine` in `agent.yaml`). On another machine every
  write action refuses, naming the journal machine; say so and stop.
- `missing` → the `harness` tool with `action="journal_missing"` (days since 2026-09-03, through yesterday,
  with activity and no block; its `missing` list is the days). The session-start
  line `journal missing for: ...` shows only the seven most recent; `journal_missing` lists all. `today`/`yesterday` → the local dates. Explicit dates as given.
- Dedupe, sort ascending, and say the list before starting. A day with no activity at all
  gets a one-line block, `- nothing recorded`, and no subagent.

## 1. Gather, do not read

- The `harness` tool with `action="journal_collect"`, `date=<date>`, `write=True` saves the bundle to
  `.agent/journal/<date>.collect.json` (gitignored, outside the vault's notes); hand the path
  over, do not read it. It carries the day's feature log entries, dispatches finished that day
  with their summaries and handoffs, ADRs, retros, the day's raw prompt heads per feature and
  as `unassigned_raw` for prompts with no feature, and the git subjects of the vault
  (`harness_git`) and the registered repos. Call `harness` with `action="scratch_path"` once for the drafts (it ensures the dirs).

## 2. Delegate the drafting

One `Agent` call per day, `subagent_type: journal-drafter` (`.claude/agents/journal-drafter.md`:
sonnet, the Claude model of the rubric's review row, a fixed brief with judgment about wording
only; the brief is
the agent's system prompt, so the call carries only the facts). The prompt is three lines: the
date, the bundle path, and the draft path `.agent/scratch/journal/<date>.md`. If the agent
definition is missing, fall back to `subagent_type: general-purpose`, `model: sonnet`, with the
definition's body pasted as the brief.

## 3. Verify before you write

Read the draft. Reject and re-run the subagent once with the failure named, then fall back to
writing it yourself from the bundle, when any of these fail:

- Not two to eight bullets, a bullet that runs to a second line, or anything that is not a
  bullet (a header, bold, a sentence of prose).
- A number outside a PR number.
- A wikilink target that does not exist: `ls <target>.md` for each `[[…]]`.
- A bullet the records do not support. Check each one against
  `jq -r '.dispatches[] | "\(.id) \(.state) \(.summary)"' <bundle>` and
  `jq -r '.features[].log_entries[].text' <bundle> | cut -c1-120`; a worker the ledger shows
  `blocked` or `question` written as done is the usual miss.
- Fluff: "successfully", "leveraged", a claim about how well it went. The owner's bullets say what
  was done.

## 4. Write and commit

- The `harness` tool with `action="journal_write"`, `date=<date>`,
  `file=.agent/scratch/journal/<date>.md` replaces the block,
  sets the numbers from every machine's usage records (`usage_machines` shows which were
  counted), and leaves Notes and any hand-added
  property byte for byte; it prints the note's path (`journal/YYYY/MM-Month/DD-Weekday.md`). Never
  edit the note by hand.
- The commit goes in the repo the note lives in, which is the notes repo when the private set
  is linked (ADR-0012): the `harness` tool with `action="notes_root"` prints it. From the vault root `git add
  journal/...` answers "fatal: pathspec is beyond a symbolic link", and the path is ignored
  there anyway.
- With `notes_sync: on` in `agent.local.yaml` (ADR-0034), stop here: the notes repo commits
  and pushes the note itself when the turn ends. With the switch off, commit by hand:
  `git -C <notes root> status --short <that path>`; if it shows: `git -C <notes root> add
  <that path>`, then `git -C <notes root> commit -m "journal: <date>"`. One commit per day.
  Paths stay relative to the notes root (`journal/YYYY/MM-Month/DD-Weekday.md`), which is what
  the `journal_write` action prints.

## 5. Report

Three lines: the days written (and any skipped as unchanged), the spend those days carry,
and what the verification rejected, if anything. The trend across days is the journal Base's
columns, not the note.
