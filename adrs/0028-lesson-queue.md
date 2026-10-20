---
id: 28
title: Findings without an open feature wait in a lesson queue
status: accepted
date: 2026-09-24
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0028: Findings without an open feature wait in a lesson queue

## Context

ADR-0024 made "tracked" the default state of a finding: "A task with a named owner and a
verifiable end state. Not an inbox." A task lives in a feature. `/lesson` and `/retro` produce
findings after their feature has closed, and a task there reopens it. A retro can produce
lessons and owner practices with no open feature to hold them, and those findings have no home
to go to.

## Decision

A finding from a closed feature, a retro, or no feature waits in a lesson queue until the owner
reviews it. A finding from an open feature still becomes a task in that feature.

This amends ADR-0024's **Tracked** paragraph (lines 35-37). It now reads: a task with a named
owner and a verifiable end state, or, when no open feature can hold it, a queue entry that
states its evidence and proposed home. The queue is triaged, so it is not the inbox that
paragraph warned about: review resolves every entry into a task, a mechanism, prose, an owner
practice, or a drop with its reason, and resolved entries stay on record.

There are two queues on the public/private split (ADR-0012): `harness/lessons/queue.md`
(tracked, and every field is refused if it names a `scrub.yaml` term) and
`harness/owner/lessons-queue.md` (private, created on first write). `agent lesson
add|list|resolve` and one routed `lesson` MCP tool write them under one lock, with ids unique
across both. The session-start block names the queue once it holds 7 open entries, and `/lesson review` works through it. `/retro` queues its lessons without a
yes per item; review is where the owner decides.

## Consequences

Closing a feature no longer depends on every lesson being homed first. The graveyard risk
ADR-0024 named moves to the queue, and the threshold nudge is the guard: if the queue sits at 7
for weeks, revisit the threshold or the review, not the queue.

Amended 2026-10-18 (owner): the nudge was not a guard. Only the model reads the session-start
block, and the queue reached 139 open entries before the owner saw it. The threshold is now 10,
and `feature close` refuses while 10 or more entries are open, so a close puts the count in
front of the owner. The manager tool count goes to
25 of the ~35 budget (ADR-0007). A public entry has to state its evidence without the private
feature it came from, which makes some entries longer.

Amended by ADR-0048: lines 34-38 now read as one local queue per install in the notes repo.

## Alternatives considered

Keep adding tasks to the retro's feature: reopens closed features, which the owner rejected. A
standing "harness backlog" feature: an open feature that never closes, with no retro and no
end. One queue with private entries redacted: a redaction is a leak that has not happened yet;
the existing split is the safer default.
