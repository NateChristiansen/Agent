# The journal

`journal/YYYY/MM-Month/DD-Weekday.md` (say `journal/2026/09-September/14-Monday.md`) is the day's note:
Obsidian's daily note and the committed record of what got done. It is written for the owner
in six months. It is short. It links out only where a reader would click through.
This file says what the note is, what writes it, and when.

## The note

```
---
tags:
- journal
date: 2026-01-14
spend_usd: 12.50
spend_complete: true
manager_usd: 8.00
manager_complete: true
dispatches: 3
session_tokens: 42000000
session_models: opus-5 30.0M, sonnet-5 10.0M, haiku-4-5 2.0M
---
# 2026-01-14

<!-- journal:generated -->
- merged the login form PR after two review rounds; the last worker fixed the focus order
- closed 003 and 004 with retros
- fixed the resume path so an empty queue-drain result is no longer read as a worker's return
<!-- /journal:generated -->

## Notes
```

**The block** between the markers is a plain bullet list of what got done that day, in the
owner's voice: lowercase, past tense, one thing per line, no headers or sections, a wikilink only
where the detail lives somewhere (a handoff, a retro, an ADR). `/journal` writes it from the
records and rewrites it on every run. Nothing in it is typed by hand.

**The properties** are set by the CLI on every write and are data, not prose.
`bases/journal.base` shows them as columns across days, and any other property the owner adds
by hand survives every write. All of them are sums over every machine's usage record (see
[[#One writer, and every machine's numbers]]):

- `spend_usd` and `dispatches`: dispatched worker spend and count, by each dispatch's `finished`
  local day. Worker spend and nothing else, which is why the Base labels it "Workers ($)".
- `manager_usd`: what the owner's own sessions cost that day, stored on each session record as
  a per-local-day split. Claude's figure is priced by Claude Code and read off its statusLine
  payload (`cost.total_cost_usd`) by `bin/agent status line`; a Codex session is priced locally
  from its rollout at each Stop ([[adrs/0022-price-codex-manager-sessions-in-dollars|ADR-0022]]).
  Session records are pruned at 14 days, so an older day keeps the figure it was written with.
- `spend_complete` and `manager_complete`: `true` when every expected machine's record carries a
  full figure for `spend_usd` or `manager_usd`; `false` means the day's figure is a lower bound.
- `session_tokens` and `session_models`: tokens the manager's sessions and their subagents used
  that day, per model, deduplicated by request. Tokens rather than dollars, because transcripts
  carry no cost; most of the volume is cache reads.
- `probe_spend_usd`: eval probe spend, kept apart from `spend_usd`. Written only on a day that
  had some, so other notes keep their usual properties.

**Notes** is the owner's. Nothing in the harness reads or writes below `## Notes`. There is no
live log in the note: the feature logs, dispatch records and handoffs already hold that, and the
note is the distillation.

## When it is written

On demand, and on a milestone. `/journal` writes today, `/journal <date>` a day, and
`/journal missing` every day since the default start (`SINCE_DEFAULT` in
`src/agent_cli/journal/block.py`) that has activity and no block. There is no
scheduled run: a note generated from records that already exist does not need a clock.

Milestones leave a marker in `.agent/journal/pending/<date>.jsonl`: a feature closing, a
dispatch returning terminal, an ADR landing, a PR's state changing, and a branch landing on
main. A marker writes nothing itself. The session-start block and the `journal-pending`
UserPromptSubmit hook inject the list with an instruction to write today's journal, the
session-start block also lists days missing a note, and `feature_close` names `/journal today`
as its next step. `journal write` clears the day's markers. A session is nudged again whenever
the pending list changes (`journal_nag_seen:<day>` holds the fingerprint it last saw), so later
landings are never hidden.

## A note created by hand

Two ways, both landing on the same skeleton (`harness/obsidian/templates/daily.md`, where
`{{date:YYYY-MM-DD}}` fills the property and the title):

- Obsidian's daily note: `obsidian setup` points the Daily notes plugin at `journal/` with
  the format `YYYY/MM-MMMM/DD-dddd` and that template, so "Open today's daily note" creates the
  right file in the right folder.
- Any note: `obsidian setup` points the core Templates plugin at `harness/obsidian/templates`,
  so "Insert template → daily" fills a note created anywhere; move it under `journal/` for the
  Base to list it.

The CLI adopts either one on the first `/journal` run for that day. It keeps the frontmatter
and whatever is under `## Notes`, fills the block, and adds the properties.

## One writer, and every machine's numbers

`journal.machine` in `agent.yaml` names the one machine that writes notes, because two machines
rewriting one note stop notes sync on a rebase conflict. `journal write` and
`journal collect --write` refuse on any other, naming both labels and the key, and that
machine's journal nudges and `feature_close` journal step stay quiet. With the key unset every
machine writes. A write holds a per-note lock (`.agent/locks/journal-<date>.lock`, not synced)
and reads the note again under it.

Each machine publishes `usage/YYYY/MM-Month/DD-Weekday.<tag>.md` and `usage/machines/<tag>.md`. The expected machines are the
checkpoint files under `usage/machines/`. `journal write` sums the records with
`usage.aggregate` and adds:

- `usage_machines`: per machine `status` (`complete`, `partial`, `unavailable`, `inactive` or
  `missing`) and `updated`. A record that fails validation is `missing`; so is a machine with
  no record and a checkpoint that stops before the day ends. A checkpoint that covers the whole
  day with no record is `inactive`.
- `tokens_complete`: true only when every machine is `complete` or `inactive`. Otherwise
  `session_tokens` is a lower bound.

A note written before usage records existed keeps its old figures until every expected machine
is counted; `usage_held: true` marks it. With no checkpoint files there are no expected
machines: `tokens_complete` is false and a held note keeps its figures. An invalid
`journal.machine` refuses every note write.

A record can arrive after the note. Run `/journal <date>` again to update the block and
frontmatter through `journal_write`. `journal_missing` still returns a `stale` list for
notes whose usage records changed; session start does not repeat that list.

## Where the pieces live

- `src/agent_cli/journal/`: the note writer (`note_rel`, `ensure_note`, `write_generated`,
  `split_sections`, `missing_days`), `collect` (one day's records bundled for the drafting
  subagent: feature log entries, captures, flags, ADRs, dispatches finished that day, retros,
  the vault's and the registered repos' git subjects), `writer.py` (the guard and the note
  lock), `usage_note.py` (expected machines, summed numbers, `stale_days`) and `tokens.py`
  (how a token count is formatted).
- `src/agent_cli/usage/`: each machine's daily record (`collect.py`, `claude.py`, `codex.py`),
  its publication, and `aggregate.py`.
- The `harness` tool's `journal_collect`, `journal_missing` and `journal_write` actions. `journal_collect` saves the bundle under `.agent/journal/`, outside
  the vault's notes.
- `.agents/skills/journal/SKILL.md`: the pass. It gathers without reading, hands the drafting
  to a subagent with a fixed brief and the owner's own sample bullets, checks every bullet
  against the bundle and every wikilink against the vault, then writes.
- `src/agent_cli/journal/pending.py`: the milestone markers under `.agent/journal/pending/`.
  `record` never raises: it fires inside `feature close`, `adr new`, a dispatch's `terminal.finalize`
  and the PR ledger's `refresh_pr`, and none of those may fail because the journal is busy.
- `bin/agent hook session-start` and `bin/agent hook journal-pending`: the nudges above.
- `src/agent_cli/vault/bases.py`: the journal Base and the Dashboard's link to today's note.
- `src/agent_cli/obsidian.py`: the Daily notes and Templates plugin config.
