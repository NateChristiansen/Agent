---
id: 48
title: One local lesson queue, triaged to upstream issues
status: accepted
date: 2026-10-09
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0048: One local lesson queue, triaged to upstream issues

## Context

People who try the harness will not open pull requests, so a harness lesson they find has no
way back to the maintainer. The tracked `harness/lessons/queue.md` also ships the maintainer's
entries to every clone and trips the lesson nudge there.

## Decision

Every install keeps one local lesson queue, `harness/owner/lessons-queue.md` in its notes
repo, and `/lesson review` decides where each lesson goes. This amends two paragraphs:
- ADR-0028 lines 34-38, up to "with ids unique across both.", now read: one local queue per
  install, written by `lesson add` under one lock; the tracked queue is retired.
- ADR-0043's `[harness-candidate]` bullet (lines 29-30) now reads: a consumer queues every
  lesson locally, and review may file it upstream.

Review gains the dispositions `upstream`, `config` and `repo`. For `upstream`, the manager
searches for duplicates, drafts the issue and shows the full text, and the user approves. The
manager then runs `gh issue create --repo <upstream> --title "[lesson] ..." --body-file <draft>`
and resolves the entry with the issue URL. The upstream is origin, or origin's fork parent. A
`harness-lesson: <id>` body marker, not a label, finds these issues, because GitHub drops labels
from filers without push access. Workers stay denied `gh issue`.

## Consequences

A fresh clone ships no lesson entries and shows no lesson nudge. The scrub check moves to filing
time and still refuses when `scrub.yaml` is missing. Setup writes an empty one, which matches
nothing, so on most installs the shown draft is the real gate. On Claude an ask rule prompts
for `gh issue create`; on Codex the approval is prose only. The maintainer has two inboxes, the
local queue and upstream issues. A one-time `lesson merge` moves the tracked entries into
notes, keeping both entries when separate machines minted the same id.

## Alternatives considered

Hash-bound drafts with pending markers: more code than the expected issue volume justifies. An
`upstream:` config key: `agent.yaml` is private, so it would not ship. Issue-form prefill:
`--body-file` skips forms. A tracked maintainer queue: it ships to every clone.
